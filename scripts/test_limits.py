#!/usr/bin/env python3
"""MANUAL limit test. Sends paid requests. Never run this from CI.

Standard library only.

Reads:
  LITELLM_BASE_URL    proxy URL
  LITELLM_MASTER_KEY  proxy admin credential (needed to create the temp keys)

What it verifies, using throwaway keys with deliberately tiny limits:
  1. a valid request on a fresh key succeeds
  2. requests are refused once max_budget is exhausted
  3. RPM overflow returns 429
  4. TPM overflow returns 429
  5. switching to a different model does NOT bypass the exhausted key budget
  6. management APIs are denied to a customer key

Every temporary key is blocked and then deleted before the script exits.

This script refuses to run when CI or GITHUB_ACTIONS is set, and requires both
the --i-understand-this-spends-money flag and an interactive typed confirmation.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

try:
    from scripts.common import (
        CUSTOMER_ACCESS_GROUP,
        ContractError,
        HttpError,
        base_url,
        http_request,
        mask_key,
        redact,
        register_environment_secrets,
        require_env,
        try_http_request,
    )
except ImportError:  # run directly: python scripts/test_limits.py
    from common import (  # type: ignore[no-redef]
        CUSTOMER_ACCESS_GROUP,
        ContractError,
        HttpError,
        base_url,
        http_request,
        mask_key,
        redact,
        register_environment_secrets,
        require_env,
        try_http_request,
    )

CONFIRMATION = "SPEND MONEY"
PRIMARY_MODEL = "claude-haiku-4-5"
SECONDARY_MODEL = "claude-sonnet-5"
CI_MARKERS = ("CI", "GITHUB_ACTIONS", "BUILD_ID", "RENDER")

FORBIDDEN_ROUTES: tuple[tuple[str, str, Any], ...] = (
    ("POST", "/key/generate", {}),
    ("GET", "/key/list", None),
    ("POST", "/model/new", {}),
)


class Results:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, message: str) -> None:
        print(f"  PASS  {message}")

    def warn(self, message: str) -> None:
        print(f"  WARN  {message}")
        self.warnings.append(message)

    def fail(self, message: str) -> None:
        print(f"  FAIL  {message}")
        self.failures.append(message)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--i-understand-this-spends-money",
        dest="confirmed_flag",
        action="store_true",
        help="required; this script sends real, billable requests to Azure",
    )
    parser.add_argument("--base-url", default=None, help="defaults to LITELLM_BASE_URL")
    parser.add_argument(
        "--key",
        default=None,
        help="use an existing throwaway key instead of creating one (still blocked on exit)",
    )
    parser.add_argument("--budget", type=float, default=0.01, help="temp key budget in USD")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--max-attempts", type=int, default=25)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args(argv)


def refuse_automated_run() -> str | None:
    for marker in CI_MARKERS:
        if os.getenv(marker):
            return marker
    return None


def confirm_interactively() -> bool:
    if not sys.stdin.isatty():
        print(
            "error: refusing to run without an interactive terminal. This script sends "
            "billable requests.",
            file=sys.stderr,
        )
        return False
    print(f'Type exactly "{CONFIRMATION}" to continue, anything else to abort.')
    try:
        answer = input("> ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() == CONFIRMATION


def create_temp_key(
    target: str,
    admin_key: str,
    *,
    alias: str,
    budget: float,
    rpm: int,
    tpm: int,
    max_parallel: int,
    timeout: float,
) -> str:
    _, _, response = http_request(
        "POST",
        f"{target}/key/generate",
        bearer=admin_key,
        timeout=timeout,
        payload={
            "key_alias": alias,
            "models": [CUSTOMER_ACCESS_GROUP],
            "max_budget": budget,
            "budget_duration": "30d",
            "rpm_limit": rpm,
            "tpm_limit": tpm,
            "max_parallel_requests": max_parallel,
        },
    )
    value = response.get("key") if isinstance(response, dict) else None
    if not isinstance(value, str) or not value:
        raise ContractError("/key/generate did not return a key value")
    print(f"  created temp key {alias} {mask_key(value)}")
    return value


def dispose_temp_key(target: str, admin_key: str, key: str, *, timeout: float) -> None:
    """Block first (takes effect immediately), then best-effort delete."""

    status, _, _ = try_http_request(
        "POST", f"{target}/key/block", bearer=admin_key, payload={"key": key}, timeout=timeout
    )
    print(f"  blocked temp key {mask_key(key)} (HTTP {status})")
    status, _, _ = try_http_request(
        "POST",
        f"{target}/key/delete",
        bearer=admin_key,
        payload={"keys": [key]},
        timeout=timeout,
    )
    print(f"  delete temp key {mask_key(key)} (HTTP {status})")


def send(target: str, key: str, model: str, max_tokens: int, timeout: float) -> int:
    status, _, _ = try_http_request(
        "POST",
        f"{target}/v1/chat/completions",
        bearer=key,
        timeout=timeout,
        payload={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": max_tokens,
        },
    )
    return status


def check_budget(
    target: str, key: str, args: argparse.Namespace, results: Results
) -> bool:
    """Returns True once the key is refused for budget reasons."""

    print("2/6 budget exhaustion blocks new requests")
    first = send(target, key, PRIMARY_MODEL, args.max_tokens, args.timeout)
    if 200 <= first < 300:
        results.ok("first request on a fresh key succeeded")
    elif first == 429:
        results.warn(
            "first request already returned 429; the budget is smaller than one "
            "request's reservation. That is still a hard stop, just an immediate one."
        )
    else:
        results.fail(f"first request returned HTTP {first}")
        return False

    for attempt in range(2, args.max_attempts + 1):
        status = send(target, key, PRIMARY_MODEL, args.max_tokens, args.timeout)
        if 200 <= status < 300:
            continue
        results.ok(f"request {attempt} refused with HTTP {status} after budget exhaustion")
        return True

    results.warn(
        f"budget not exhausted within {args.max_attempts} requests. Lower --budget or raise "
        "--max-tokens and re-run; this check proved nothing."
    )
    return False


def check_model_switch_does_not_bypass(
    target: str, key: str, args: argparse.Namespace, results: Results
) -> None:
    print("3/6 switching models does not bypass the key budget")
    status = send(target, key, SECONDARY_MODEL, args.max_tokens, args.timeout)
    if 200 <= status < 300:
        results.fail(
            f"{SECONDARY_MODEL} was served (HTTP {status}) on a budget-exhausted key: "
            "the budget is not enforced across models"
        )
    else:
        results.ok(f"{SECONDARY_MODEL} also refused with HTTP {status}")


def check_rpm(
    target: str, admin_key: str, args: argparse.Namespace, results: Results, disposables: list[str]
) -> None:
    print("4/6 RPM overflow returns 429")
    key = create_temp_key(
        target,
        admin_key,
        alias="temp-limit-rpm",
        budget=args.budget,
        rpm=1,
        tpm=1_000_000,
        max_parallel=4,
        timeout=args.timeout,
    )
    disposables.append(key)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(
            pool.map(lambda _: send(target, key, PRIMARY_MODEL, 16, args.timeout), range(4))
        )
    print(f"  statuses: {statuses}")
    if 429 in statuses:
        results.ok("rpm_limit=1 produced a 429")
    else:
        results.fail(f"no 429 seen with rpm_limit=1; statuses were {statuses}")


def check_tpm(
    target: str, admin_key: str, args: argparse.Namespace, results: Results, disposables: list[str]
) -> None:
    print("5/6 TPM overflow returns 429")
    key = create_temp_key(
        target,
        admin_key,
        alias="temp-limit-tpm",
        budget=args.budget,
        rpm=1_000,
        tpm=50,
        max_parallel=4,
        timeout=args.timeout,
    )
    disposables.append(key)
    statuses = [send(target, key, PRIMARY_MODEL, 64, args.timeout) for _ in range(4)]
    print(f"  statuses: {statuses}")
    if 429 in statuses:
        results.ok("tpm_limit=50 produced a 429")
    else:
        results.fail(f"no 429 seen with tpm_limit=50; statuses were {statuses}")


def check_management_denied(
    target: str, key: str, args: argparse.Namespace, results: Results
) -> None:
    print("6/6 management APIs denied to a customer key")
    for method, path, payload in FORBIDDEN_ROUTES:
        status, _, _ = try_http_request(
            method, f"{target}{path}", bearer=key, payload=payload, timeout=args.timeout
        )
        if 200 <= status < 300:
            results.fail(f"{method} {path} was ACCEPTED (HTTP {status})")
        elif status in (401, 403):
            results.ok(f"{method} {path} denied with HTTP {status}")
        else:
            results.warn(f"{method} {path} returned HTTP {status}; not an explicit denial")


def main(argv: list[str] | None = None) -> int:
    register_environment_secrets()
    args = parse_args(argv)

    marker = refuse_automated_run()
    if marker:
        print(
            f"error: {marker} is set. This script sends billable requests and must never run "
            "in an automated pipeline.",
            file=sys.stderr,
        )
        return 2
    if not args.confirmed_flag:
        print(
            "error: pass --i-understand-this-spends-money. Every check below sends real, "
            "billable requests to Azure AI Foundry.",
            file=sys.stderr,
        )
        return 2
    if args.budget <= 0 or args.max_tokens <= 0 or args.max_attempts <= 0:
        print("error: --budget, --max-tokens and --max-attempts must all be positive.", file=sys.stderr)
        return 2
    if not confirm_interactively():
        print("aborted; no requests were sent.")
        return 1

    try:
        target = base_url(args.base_url)
        admin_key = require_env("LITELLM_MASTER_KEY")
    except ContractError as error:
        print(f"error: {redact(error)}", file=sys.stderr)
        return 2

    results = Results()
    disposables: list[str] = []
    try:
        print("1/6 provision throwaway budget key")
        if args.key:
            budget_key = args.key
            print(f"  using supplied key {mask_key(budget_key)}")
        else:
            budget_key = create_temp_key(
                target,
                admin_key,
                alias="temp-limit-budget",
                budget=args.budget,
                rpm=60,
                tpm=1_000_000,
                max_parallel=1,
                timeout=args.timeout,
            )
        disposables.append(budget_key)
        # Give the proxy a moment to make the new key visible to every check.
        time.sleep(2)

        exhausted = check_budget(target, budget_key, args, results)
        if exhausted:
            check_model_switch_does_not_bypass(target, budget_key, args, results)
        else:
            results.warn("skipped the model-switch check: budget was never exhausted")

        check_rpm(target, admin_key, args, results, disposables)
        check_tpm(target, admin_key, args, results, disposables)
        check_management_denied(target, budget_key, args, results)
    except (HttpError, ContractError) as error:
        results.fail(f"aborted: {redact(error)}")
    finally:
        print("\ncleanup")
        for key in disposables:
            try:
                dispose_temp_key(target, admin_key, key, timeout=args.timeout)
            except Exception as error:  # noqa: BLE001 - cleanup must never mask results
                print(f"  WARN could not dispose {mask_key(key)}: {redact(error)}")

    print("\n=== summary ===")
    for warning in results.warnings:
        print(f"WARN  {warning}")
    if results.failures:
        for failure in results.failures:
            print(f"FAIL  {failure}")
        return 1
    print("all limit checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
