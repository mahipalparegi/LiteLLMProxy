#!/usr/bin/env python3
"""Post-deployment smoke test, run with a CUSTOMER virtual key.

Standard library only.

Reads:
  LITELLM_BASE_URL  proxy URL
  LITELLM_API_KEY   a customer virtual key (never the master key)

Checks, in order:
  1. GET  /health/liveliness            (unauthenticated)
  2. GET  /health/readiness             (unauthenticated, db must be connected)
  3. GET  /v1/models                    (every expected alias present)
  4. POST /v1/chat/completions          (gpt-5.6, max 20 output tokens)
  5. An invalid model name is rejected
  6. The virtual key is denied on key management, model management, config and
     admin routes

Step 4 costs a few tokens. Use --skip-generation to omit it.
No load, budget or rate-limit testing happens here; that is test_limits.py,
which is manual by design.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

try:
    from scripts.common import (
        PROXY_ALIASES,
        ContractError,
        HttpError,
        base_url,
        http_request,
        mask_key,
        missing_aliases,
        model_entries,
        public_model_name,
        redact,
        register_environment_secrets,
        require_env,
        try_http_request,
    )
except ImportError:  # run directly: python scripts/smoke_test.py
    from common import (  # type: ignore[no-redef]
        PROXY_ALIASES,
        ContractError,
        HttpError,
        base_url,
        http_request,
        mask_key,
        missing_aliases,
        model_entries,
        public_model_name,
        redact,
        register_environment_secrets,
        require_env,
        try_http_request,
    )

TEST_MODEL = "gpt-5.6"
MAX_OUTPUT_TOKENS = 20
PROMPT = "Reply with the single word OK."

# Bodies cannot create or change anything even if a call were unexpectedly
# authorised. On model-management routes LiteLLM validates the body before it
# checks authorization, so an incomplete body returns 400/422 rather than 403;
# that is reported as a warning, never as a pass. See MANUAL_AUTHZ_PROBE.
FORBIDDEN_ROUTES: tuple[tuple[str, str, Any], ...] = (
    ("POST", "/key/generate", {}),
    ("GET", "/key/list", None),
    ("POST", "/model/new", {"model_name": "", "litellm_params": {}}),
    ("POST", "/model/delete", {"id": "00000000-0000-0000-0000-000000000000"}),
    ("POST", "/config/update", {}),
    ("POST", "/global/spend/reset", None),
)

MANUAL_AUTHZ_PROBE = """
To confirm model-management authorization directly, send a fully valid body with
the customer key. Expect HTTP 403 with role=internal_user:

  curl -sS -o /dev/null -w '%{http_code}\\n' -X POST "$LITELLM_BASE_URL/model/new" \\
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'content-type: application/json' \\
    -d '{"model_name":"authz-probe","litellm_params":{"model":"azure/gpt-5.6"}}'

This script does not send it automatically: a valid body would create a model if
the script were ever run with an admin key by mistake.
""".strip()


class Report:
    """Collects pass/fail/warn results and renders a summary."""

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
    parser.add_argument("--base-url", default=None, help="defaults to LITELLM_BASE_URL")
    parser.add_argument("--model", default=TEST_MODEL, help=f"default {TEST_MODEL}")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--skip-generation",
        action="store_true",
        help="skip the token-consuming request (step 4)",
    )
    return parser.parse_args(argv)


def check_health(target: str, report: Report, timeout: float) -> None:
    print("1/6 probe endpoints")
    for path in ("/health/liveliness", "/health/readiness"):
        status, _, body = try_http_request("GET", f"{target}{path}", timeout=timeout)
        if status != 200:
            report.fail(f"{path} returned HTTP {status}")
            continue
        if path.endswith("readiness") and isinstance(body, dict):
            database = str(body.get("db", ""))
            if database.lower() != "connected":
                report.fail(f"{path} reports db={database!r}; Postgres is mandatory")
                continue
            report.ok(f"{path} healthy, database connected")
        else:
            report.ok(f"{path} responded 200")


def check_models(target: str, key: str, report: Report, timeout: float) -> None:
    print("2/6 model list")
    try:
        _, _, payload = http_request("GET", f"{target}/v1/models", bearer=key, timeout=timeout)
    except (HttpError, ContractError) as error:
        report.fail(f"/v1/models failed: {redact(error)}")
        return
    names = [name for entry in model_entries(payload) if (name := public_model_name(entry))]
    if not names:
        report.fail("/v1/models returned no models for this key")
        return
    absent = missing_aliases(names)
    if absent:
        report.fail("/v1/models is missing aliases: " + ", ".join(absent))
    else:
        report.ok(f"all {len(PROXY_ALIASES)} aliases visible ({len(names)} models total)")


def check_chat_completions(target: str, key: str, model: str, report: Report, timeout: float) -> None:
    print("3/6 POST /v1/chat/completions")
    try:
        _, headers, body = http_request(
            "POST",
            f"{target}/v1/chat/completions",
            bearer=key,
            timeout=timeout,
            payload={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": MAX_OUTPUT_TOKENS,
            },
        )
    except (HttpError, ContractError) as error:
        report.fail(f"/v1/chat/completions failed: {redact(error)}")
        return
    if not isinstance(body, dict) or not body.get("choices"):
        report.fail("/v1/chat/completions returned no choices")
        return
    cost = headers.get("x-litellm-response-cost")
    report.ok(f"/v1/chat/completions answered (recorded cost header: {cost or 'absent'})")
    if not cost:
        report.warn(
            "no x-litellm-response-cost header: run verify_models_and_costs.py before "
            "trusting USD budgets"
        )


def check_invalid_model(target: str, key: str, report: Report, timeout: float) -> None:
    print("4/6 invalid model is rejected")
    status, _, _ = try_http_request(
        "POST",
        f"{target}/v1/chat/completions",
        bearer=key,
        timeout=timeout,
        payload={
            "model": "definitely-not-a-configured-model",
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 1,
        },
    )
    if 200 <= status < 300:
        report.fail(f"an unconfigured model was accepted (HTTP {status})")
    else:
        report.ok(f"unconfigured model rejected with HTTP {status}")


def check_forbidden_routes(target: str, key: str, report: Report, timeout: float) -> None:
    print("5/6 management, config and admin routes are denied")
    for method, path, payload in FORBIDDEN_ROUTES:
        status, _, _ = try_http_request(
            method, f"{target}{path}", bearer=key, payload=payload, timeout=timeout
        )
        if 200 <= status < 300:
            report.fail(f"{method} {path} was ACCEPTED (HTTP {status}) with a customer key")
        elif status in (401, 403):
            report.ok(f"{method} {path} denied with HTTP {status}")
        elif status in (404, 405):
            report.warn(
                f"{method} {path} returned HTTP {status}: route not present on this build, "
                "nothing proven either way"
            )
        elif status in (400, 422):
            report.warn(
                f"{method} {path} returned HTTP {status}: the body was rejected before "
                "authorization ran, so nothing was created or changed, but denial is not "
                "proven. Expected for model-management routes."
            )
        else:
            report.warn(
                f"{method} {path} returned HTTP {status}: request had no effect, but this is "
                "not an explicit 401/403 denial. Confirm manually."
            )


def main(argv: list[str] | None = None) -> int:
    register_environment_secrets()
    args = parse_args(argv)

    try:
        target = base_url(args.base_url)
        key = require_env("LITELLM_API_KEY")
    except ContractError as error:
        print(f"error: {redact(error)}", file=sys.stderr)
        return 2

    print(f"proxy:  {target}")
    print(f"key:    {mask_key(key)}")
    print(f"model:  {args.model}\n")

    report = Report()
    check_health(target, report, args.timeout)
    check_models(target, key, report, args.timeout)
    if args.skip_generation:
        print("3/6 POST /v1/chat/completions  SKIPPED (--skip-generation)")
    else:
        check_chat_completions(target, key, args.model, report, args.timeout)
    check_invalid_model(target, key, report, args.timeout)
    check_forbidden_routes(target, key, report, args.timeout)

    print("\n6/6 summary")
    if report.warnings:
        print(f"  {len(report.warnings)} warning(s) - review them before going live")
        if any("authorization ran" in warning for warning in report.warnings):
            print(f"\n{MANUAL_AUTHZ_PROBE}\n")
    if report.failures:
        print(f"  {len(report.failures)} failure(s):")
        for failure in report.failures:
            print(f"    - {failure}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
