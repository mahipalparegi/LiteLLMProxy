#!/usr/bin/env python3
"""Prove the proxy serves N concurrent API callers. Default target is 20.

Standard library only.

Reads:
  LITELLM_BASE_URL  proxy URL
  LITELLM_API_KEY   a customer virtual key (never the master key)

Two modes:

  default (free)   N simultaneous GET /v1/models. Exercises TLS, key auth and
                   model-access resolution, sends nothing to Azure, costs nothing.
                   Proves gateway capacity, NOT that the key's
                   max_parallel_requests allows N concurrent generations.

  --generate       N simultaneous POST /v1/chat/completions. Billable, and the
                   only mode subject to the key's max_parallel_requests, rpm_limit
                   and tpm_limit and to your Foundry quota. Requires
                   --i-understand-this-spends-money and never runs in CI.

A 429 in --generate mode is not a proxy fault: the key's limits or the Foundry
quota sit below the target.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from scripts.common import (
        ContractError,
        base_url,
        mask_key,
        positive_number,
        redact,
        register_environment_secrets,
        require_env,
        try_http_request,
    )
except ImportError:  # run directly: python scripts/concurrency_check.py
    from common import (  # type: ignore[no-redef]
        ContractError,
        base_url,
        mask_key,
        positive_number,
        redact,
        register_environment_secrets,
        require_env,
        try_http_request,
    )

DEFAULT_CONCURRENCY = 20
GENERATE_MODEL = "gpt-5.6-sol"
GENERATE_MAX_TOKENS = 16
CI_MARKERS = ("CI", "GITHUB_ACTIONS")


def _positive_int(raw: str) -> int:
    try:
        return int(positive_number(raw, name="value", integer=True))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=DEFAULT_CONCURRENCY,
        help=f"simultaneous callers to simulate (default {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--rounds",
        type=_positive_int,
        default=3,
        help="how many times to repeat the burst (default 3)",
    )
    parser.add_argument("--base-url", default=None, help="defaults to LITELLM_BASE_URL")
    parser.add_argument("--model", default=GENERATE_MODEL, help=f"default {GENERATE_MODEL}")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--generate",
        action="store_true",
        help="send real, billable completions instead of free /v1/models reads",
    )
    parser.add_argument(
        "--i-understand-this-spends-money",
        dest="confirmed",
        action="store_true",
        help="required with --generate",
    )
    return parser.parse_args(argv)


def one_request(
    target: str, key: str, generate: bool, model: str, timeout: float
) -> tuple[int, float]:
    started = time.perf_counter()
    if generate:
        status, _, _ = try_http_request(
            "POST",
            f"{target}/v1/chat/completions",
            bearer=key,
            timeout=timeout,
            payload={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": GENERATE_MAX_TOKENS,
            },
        )
    else:
        status, _, _ = try_http_request(
            "GET", f"{target}/v1/models", bearer=key, timeout=timeout
        )
    return status, time.perf_counter() - started


def run_round(
    target: str, key: str, args: argparse.Namespace
) -> tuple[list[int], list[float], float]:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(
            pool.map(
                lambda _: one_request(target, key, args.generate, args.model, args.timeout),
                range(args.concurrency),
            )
        )
    wall = time.perf_counter() - started
    return [status for status, _ in results], [latency for _, latency in results], wall


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))
    return ordered[index]


def main(argv: list[str] | None = None) -> int:
    register_environment_secrets()
    args = parse_args(argv)

    if args.generate:
        for marker in CI_MARKERS:
            if os.getenv(marker):
                print(
                    f"error: {marker} is set. --generate sends billable requests and must "
                    "never run in an automated pipeline.",
                    file=sys.stderr,
                )
                return 2
        if not args.confirmed:
            print(
                "error: --generate needs --i-understand-this-spends-money. It sends "
                f"{args.concurrency * args.rounds} billable requests.",
                file=sys.stderr,
            )
            return 2

    try:
        target = base_url(args.base_url)
        key = require_env("LITELLM_API_KEY")
    except ContractError as error:
        print(f"error: {redact(error)}", file=sys.stderr)
        return 2

    mode = "billable completions" if args.generate else "free /v1/models reads"
    print(f"proxy:       {target}")
    print(f"key:         {mask_key(key)}")
    print(f"mode:        {mode}")
    print(f"concurrency: {args.concurrency} x {args.rounds} round(s)\n")

    statuses: list[int] = []
    latencies: list[float] = []
    for index in range(1, args.rounds + 1):
        round_statuses, round_latencies, wall = run_round(target, key, args)
        statuses.extend(round_statuses)
        latencies.extend(round_latencies)
        served = sum(1 for status in round_statuses if 200 <= status < 300)
        print(
            f"round {index}: {served}/{args.concurrency} served in {wall:.2f}s "
            f"(p50 {percentile(round_latencies, 0.50):.2f}s, "
            f"p95 {percentile(round_latencies, 0.95):.2f}s, "
            f"max {max(round_latencies):.2f}s)"
        )

    total = len(statuses)
    served = sum(1 for status in statuses if 200 <= status < 300)
    throttled = sum(1 for status in statuses if status == 429)
    other = total - served - throttled

    print(f"\nserved {served}/{total}, throttled(429) {throttled}, other failures {other}")
    print(f"latency p50 {percentile(latencies, 0.50):.2f}s  p95 {percentile(latencies, 0.95):.2f}s")
    print(f"        mean {statistics.fmean(latencies):.2f}s  max {max(latencies):.2f}s")

    if other:
        codes = sorted({s for s in statuses if not (200 <= s < 300) and s != 429})
        print(f"\nFAIL  {other} request(s) failed with status codes {codes}.")
        return 1
    if throttled:
        print(
            f"\nFAIL  {throttled} request(s) were throttled at concurrency {args.concurrency}."
        )
        if args.generate:
            print(
                "      The gateway is not the limit here. Either the key's "
                "max_parallel_requests / rpm_limit / tpm_limit is below the target, or the\n"
                "      Foundry deployment quota is. Raise the key limits (or the quota), or "
                "lower --concurrency."
            )
        else:
            print("      Unexpected on a free read; check the proxy logs.")
        return 1

    print(f"\nPASS  {args.concurrency} concurrent callers served with no throttling.")
    if not args.generate:
        print(
            "      This proves gateway capacity only. Re-run with --generate "
            "--i-understand-this-spends-money\n"
            "      to prove a customer key can actually run that many generations at once."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
