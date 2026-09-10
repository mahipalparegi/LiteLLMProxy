#!/usr/bin/env python3
"""Verify every configured model actually works AND records a real USD cost.

Standard library only.

Reads:
  LITELLM_BASE_URL    proxy URL
  LITELLM_MASTER_KEY  proxy admin credential (never printed)

A model may not join the customer-models access group until it passes here.

Routes used:
  GET  /model/info                     configured models + mapped pricing
  POST /v1/chat/completions            smallest valid chat request
  GET  /spend/logs?request_id=<id>     recorded spend for one request
  response header x-litellm-response-cost

Only models whose model_info.mode is chat or completion are exercised; every other
mode uses a different endpoint and is skipped with a warning. Run this after
renaming the config aliases to your real Foundry deployment names.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

try:
    from scripts.common import (
        CUSTOMER_ACCESS_GROUP,
        PROXY_ALIASES,
        access_groups,
        is_azure_backed,
        token_limit_param,
        ContractError,
        HttpError,
        base_url,
        http_request,
        missing_aliases,
        model_entries,
        public_model_name,
        redact,
        register_environment_secrets,
        require_env,
        underlying_model,
    )
except ImportError:  # run directly: python scripts/verify_models_and_costs.py
    from common import (  # type: ignore[no-redef]
        CUSTOMER_ACCESS_GROUP,
        PROXY_ALIASES,
        access_groups,
        is_azure_backed,
        token_limit_param,
        ContractError,
        HttpError,
        base_url,
        http_request,
        missing_aliases,
        model_entries,
        public_model_name,
        redact,
        register_environment_secrets,
        require_env,
        underlying_model,
    )

CALLABLE_MODES = ("chat", "completion")
SMALLEST_MAX_TOKENS = 4
SMALLEST_PROMPT = "Say OK."

MANUAL_COST_CHECK = """
Could not read a recorded cost from the API for this request.

Verify it manually in the Admin UI before exposing the model:
  1. Open <proxy-url>/ui and sign in as the proxy admin.
  2. Go to Logs, find the request by its id.
  3. Confirm the Cost column shows a non-zero USD value.
  4. Go to Models + Endpoints -> All Models and confirm the input and output
     cost per token are non-zero and match the official Microsoft Foundry /
     Azure AI pricing page for your contract.
If the cost is zero or blank, the model has no usable price and USD budgets
cannot be enforced for it. Keep it away from customer keys.
""".strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default=None, help="defaults to LITELLM_BASE_URL")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="check configured models and mapped pricing without sending any request",
    )
    parser.add_argument(
        "--spend-log-retries",
        type=int,
        default=6,
        help="spend logs are written in batches; how many times to poll (default 6)",
    )
    parser.add_argument("--spend-log-delay", type=float, default=5.0)
    return parser.parse_args(argv)


def mapped_pricing(entry: dict[str, Any]) -> tuple[Any, Any]:
    info = entry.get("model_info")
    info = info if isinstance(info, dict) else {}
    return info.get("input_cost_per_token"), info.get("output_cost_per_token")


def declared_mode(entry: dict[str, Any]) -> str:
    info = entry.get("model_info")
    if isinstance(info, dict):
        mode = info.get("mode")
        if isinstance(mode, str) and mode:
            return mode.lower()
    return "chat"


def is_usable_price(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def cost_from_headers(headers: dict[str, str]) -> float | None:
    raw = headers.get("x-litellm-response-cost")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def cost_from_spend_logs(
    target: str, admin_key: str, request_id: str, *, timeout: float, retries: int, delay: float
) -> float | None:
    """Poll GET /spend/logs?request_id=... for the recorded spend of one call."""

    for attempt in range(max(retries, 1)):
        if attempt:
            time.sleep(delay)
        try:
            _, _, body = http_request(
                "GET",
                f"{target}/spend/logs?request_id={request_id}",
                bearer=admin_key,
                timeout=timeout,
            )
        except (HttpError, ContractError):
            return None
        rows = body if isinstance(body, list) else [body] if isinstance(body, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("spend", "response_cost"):
                value = row.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
    return None


def verify(args: argparse.Namespace) -> int:
    target = base_url(args.base_url)
    admin_key = require_env("LITELLM_MASTER_KEY")

    try:
        _, _, info = http_request(
            "GET", f"{target}/model/info", bearer=admin_key, timeout=args.timeout
        )
    except HttpError as error:
        raise ContractError(
            f"GET /model/info returned HTTP {error.status}. Without it the configured model "
            "list cannot be read. Verify manually in the Admin UI: Models + Endpoints -> "
            "All Models."
        ) from None

    entries = model_entries(info)
    if not entries:
        raise ContractError("GET /model/info returned no configured models")

    names = [public_model_name(entry) or "<unnamed>" for entry in entries]
    print(f"configured models ({len(entries)}): {', '.join(sorted(names))}")

    absent = missing_aliases(names)
    if absent:
        print(f"FAIL  expected aliases missing from /model/info: {', '.join(absent)}")
    else:
        print(f"PASS  all {len(PROXY_ALIASES)} expected aliases present")

    failures: list[str] = list(absent)
    warnings: list[str] = []
    approved: list[str] = []

    for entry in entries:
        name = public_model_name(entry) or "<unnamed>"
        underlying = underlying_model(entry)
        mode = declared_mode(entry)
        exposed = CUSTOMER_ACCESS_GROUP in access_groups(entry)
        visibility = "customer-visible" if exposed else "hidden from customer keys"
        print(f"\n-- {name} (mode={mode}, litellm model={underlying or 'unreported'}, {visibility})")

        if underlying is not None and not is_azure_backed(underlying):
            message = f"{name}: not Azure-backed ({underlying})"
            if exposed:
                failures.append(message)
                print(
                    "   FAIL  not Azure-backed but in the customer access group: customers "
                    "would reach a provider billed outside your Azure invoice"
                )
            else:
                warnings.append(message)
                print("   WARN  not Azure-backed, but not in the customer access group")
            continue

        input_cost, output_cost = mapped_pricing(entry)
        if not is_usable_price(input_cost) or not is_usable_price(output_cost):
            failures.append(
                f"{name}: unusable mapped pricing "
                f"(input={input_cost!r}, output={output_cost!r})"
            )
            print(
                "   FAIL  no usable per-token price. Add an override from the official "
                "Microsoft Foundry / Azure AI pricing page:\n"
                "         model_info: {input_cost_per_token: ..., output_cost_per_token: ...}\n"
                "         Never use 0 - LiteLLM skips budget checks for zero-cost models."
            )
            continue
        print(f"   PASS  mapped pricing input={input_cost} output={output_cost} per token")

        if mode not in CALLABLE_MODES:
            warnings.append(f"{name}: mode={mode} not exercised")
            print(
                f"   WARN  mode={mode} uses a different route than /v1/chat/completions. "
                "This script does not call it; verify that route and its recorded cost "
                "manually before exposing the model."
            )
            continue

        if args.metadata_only:
            warnings.append(f"{name}: live request skipped (--metadata-only)")
            print("   WARN  live request skipped; recorded cost not confirmed")
            continue

        limit_param = token_limit_param(name)
        if limit_param != "max_tokens":
            print(
                f"   note  sending {limit_param}: this model rejects max_tokens, and on "
                "v1.99.0 LiteLLM does not rewrite it for you"
            )

        try:
            _, headers, body = http_request(
                "POST",
                f"{target}/v1/chat/completions",
                bearer=admin_key,
                timeout=args.timeout,
                payload={
                    "model": name,
                    "messages": [{"role": "user", "content": SMALLEST_PROMPT}],
                    token_limit_param(name): SMALLEST_MAX_TOKENS,
                },
            )
        except (HttpError, ContractError) as error:
            failures.append(f"{name}: request failed: {redact(error)}")
            print(f"   FAIL  request failed: {redact(error)}")
            continue

        if not isinstance(body, dict) or not body.get("choices"):
            failures.append(f"{name}: response contained no choices")
            print("   FAIL  response contained no choices")
            continue
        print("   PASS  answered a real request")

        header_cost = cost_from_headers(headers)
        request_id = body.get("id") if isinstance(body.get("id"), str) else None
        logged_cost = (
            cost_from_spend_logs(
                target,
                admin_key,
                request_id,
                timeout=args.timeout,
                retries=args.spend_log_retries,
                delay=args.spend_log_delay,
            )
            if request_id
            else None
        )

        costs = [value for value in (header_cost, logged_cost) if value is not None]
        if not costs:
            failures.append(f"{name}: no recorded cost from header or /spend/logs")
            print(f"   FAIL  no recorded cost.\n{_indent(MANUAL_COST_CHECK)}")
            continue
        if any(value <= 0 for value in costs):
            failures.append(
                f"{name}: recorded cost was zero or negative "
                f"(header={header_cost!r}, spend_log={logged_cost!r})"
            )
            print(
                f"   FAIL  recorded cost header={header_cost!r} spend_log={logged_cost!r}. "
                "A zero cost means budgets cannot be enforced for this model."
            )
            continue

        print(f"   PASS  recorded cost header={header_cost!r} spend_log={logged_cost!r}")
        if logged_cost is None:
            warnings.append(f"{name}: /spend/logs did not return a cost in time")
            print(
                "   WARN  /spend/logs did not return a row in time (spend logs are written "
                "in batches). Confirm in the Admin UI Logs page."
            )
        approved.append(name)

    print("\n=== summary ===")
    print(f"approved for customer exposure: {', '.join(approved) if approved else 'none'}")
    for warning in warnings:
        print(f"WARN  {warning}")
    for failure in failures:
        print(f"FAIL  {failure}")
    if failures:
        print(
            f"\nRemove a failing model from the {CUSTOMER_ACCESS_GROUP!r} access group in "
            "config.yaml. It can stay configured and keep failing here; it just must not be "
            "reachable by a customer key until it passes."
        )
        return 1
    if warnings:
        print("\nAll checks that ran passed, but review every WARN above before going live.")
    return 0


def _indent(text: str, prefix: str = "         ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main(argv: list[str] | None = None) -> int:
    register_environment_secrets()
    args = parse_args(argv)
    try:
        return verify(args)
    except ContractError as error:
        print(f"error: {redact(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
