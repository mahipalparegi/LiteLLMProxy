#!/usr/bin/env python3
"""Create standalone customer virtual keys on the LiteLLM Proxy.

Standard library only.

Reads:
  LITELLM_BASE_URL    proxy URL, e.g. https://litellm-azure-proxy.onrender.com
  LITELLM_MASTER_KEY  proxy admin key (never printed, never logged)

Each key is standalone (no team_id) and carries its own USD budget, RPM, TPM and
max parallel request limits. Two modes:

  --profiles FILE   per-customer: each customer gets their OWN selected models
                    and their OWN budget. This is the usual production mode.
  flag mode         N identical keys, each granted the whole customer catalogue.

Model access is two-layered. The "customer-models" access group is the catalogue
of models approved for customer use at all; a profile then names the subset that
customer bought. A model absent from the catalogue cannot be granted to anyone,
and a model added to config.yaml later reaches no existing key automatically.

Endpoint and field names verified against LiteLLM v1.99.0:
  POST /key/generate  - models, key_alias, max_budget, budget_duration,
                        rpm_limit, tpm_limit, max_parallel_requests
  GET  /health/readiness, GET /v1/models, GET /model/info
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:  # imported as scripts.create_virtual_keys, or run with python -m
    from scripts.common import (
        CUSTOMER_ACCESS_GROUP,
        ContractError,
        HttpError,
        access_groups,
        base_url,
        http_request,
        is_azure_backed,
        mask_key,
        missing_aliases,
        model_entries,
        positive_number,
        public_model_name,
        redact,
        register_environment_secrets,
        require_env,
        underlying_model,
        write_secret_json,
    )
except ImportError:  # run directly: python scripts/create_virtual_keys.py
    from common import (  # type: ignore[no-redef]
        CUSTOMER_ACCESS_GROUP,
        ContractError,
        HttpError,
        access_groups,
        base_url,
        http_request,
        is_azure_backed,
        mask_key,
        missing_aliases,
        model_entries,
        positive_number,
        public_model_name,
        redact,
        register_environment_secrets,
        require_env,
        underlying_model,
        write_secret_json,
    )

DEFAULT_OUTPUT = "generated-keys.json"
BUDGET_DURATION_PATTERN = re.compile(r"^[1-9][0-9]*(s|m|h|d)$")

MANUAL_PROVIDER_CHECK = """
Cannot confirm from the API which provider backs each model in the
customer-models access group.

Do this manually before issuing any customer key, then re-run with
--skip-provider-check only if you have completed it:

  1. Open the Admin UI at <proxy-url>/ui and sign in as the proxy admin.
  2. Go to Models + Endpoints -> All Models.
  3. For every model in the customer-models group, confirm the underlying
     litellm model begins with 'azure_ai/' (Foundry Anthropic) or 'azure/'
     (Azure OpenAI), and that its api_base is your own Foundry resource.
  4. Remove any non-Azure model from the group before issuing keys. Anything in
     the group is reachable by every customer key that holds it, and a non-Azure
     provider also means a second bill outside your Azure invoice.
""".strip()


def _positive_int(raw: str) -> int:
    try:
        return int(positive_number(raw, name="value", integer=True))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _positive_float(raw: str) -> float:
    try:
        return float(positive_number(raw, name="value"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def validate_budget_duration(value: str) -> str:
    """Accept LiteLLM's documented budget_duration forms: 30s, 30m, 30h, 30d."""

    text = (value or "").strip()
    if not BUDGET_DURATION_PATTERN.match(text):
        raise ValueError(
            f"--budget-duration must be a positive count followed by s, m, h or d "
            f"(for example 30d), got {value!r}"
        )
    return text


PROFILE_FIELDS = {
    "key_alias",
    "models",
    "max_budget",
    "budget_duration",
    "rpm_limit",
    "tpm_limit",
    "max_parallel_requests",
}
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


def load_profiles(path: str | Path) -> list[dict[str, Any]]:
    """Read and fully validate a per-customer profile file.

    Unknown fields are rejected rather than ignored: a typo such as
    "max_budgett" would otherwise create a key with no budget at all.
    """

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from None
    except json.JSONDecodeError as error:
        raise ContractError(f"{path} is not valid JSON: {error}") from None

    if not isinstance(raw, list) or not raw:
        raise ContractError(f"{path} must be a non-empty JSON array of customer profiles")

    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        where = f"{path} entry {index}"
        if not isinstance(entry, dict):
            raise ContractError(f"{where} must be a JSON object")

        unknown = set(entry) - PROFILE_FIELDS
        if unknown:
            raise ContractError(
                f"{where} has unrecognised field(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(PROFILE_FIELDS))}. Refusing to continue, because "
                "a misspelled limit would silently leave that limit unset."
            )
        missing = PROFILE_FIELDS - set(entry)
        if missing:
            raise ContractError(f"{where} is missing required field(s): {', '.join(sorted(missing))}")

        alias = entry["key_alias"]
        if not isinstance(alias, str) or not ALIAS_PATTERN.match(alias):
            raise ContractError(
                f"{where} key_alias must be 2-64 chars of letters, digits, dot, dash or "
                f"underscore, got {alias!r}"
            )
        if alias in seen:
            raise ContractError(f"{where} duplicates key_alias {alias!r}")
        seen.add(alias)

        models = entry["models"]
        if not isinstance(models, list) or not models:
            raise ContractError(f"{where} models must be a non-empty list of model names")
        for model in models:
            if not isinstance(model, str) or not model.strip():
                raise ContractError(f"{where} models contains an empty entry")
        if len(set(models)) != len(models):
            raise ContractError(f"{where} models contains duplicates")

        try:
            profiles.append(
                {
                    "key_alias": alias,
                    "models": list(models),
                    "max_budget": positive_number(entry["max_budget"], name="max_budget"),
                    "budget_duration": validate_budget_duration(entry["budget_duration"]),
                    "rpm_limit": positive_number(entry["rpm_limit"], name="rpm_limit", integer=True),
                    "tpm_limit": positive_number(entry["tpm_limit"], name="tpm_limit", integer=True),
                    "max_parallel_requests": positive_number(
                        entry["max_parallel_requests"],
                        name="max_parallel_requests",
                        integer=True,
                    ),
                }
            )
        except ValueError as error:
            raise ContractError(f"{where}: {error}") from None
    return profiles


def check_requested_models(profiles: list[dict[str, Any]], eligible: set[str]) -> None:
    """Refuse to grant a model that is not approved for customer use.

    `eligible` is every model carrying the customer access group, which is the
    catalogue verify_models_and_costs.py gates. A profile may also name the group
    itself to mean "everything currently in the catalogue".
    """

    problems: list[str] = []
    for profile in profiles:
        for model in profile["models"]:
            if model == CUSTOMER_ACCESS_GROUP or model in eligible:
                continue
            problems.append(f"{profile['key_alias']} -> {model}")
    if problems:
        raise ContractError(
            "Refusing to issue keys. These profiles request models that are not in the "
            f"{CUSTOMER_ACCESS_GROUP!r} catalogue:\n  "
            + "\n  ".join(problems)
            + "\n\nEligible models: "
            + (", ".join(sorted(eligible)) or "(none)")
            + f"\n\nEither fix the profile, or add the model to the {CUSTOMER_ACCESS_GROUP!r} "
            "access group in config.yaml once verify_models_and_costs.py passes for it."
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--profiles",
        default=None,
        help="JSON file of per-customer profiles: own models, budget and limits",
    )
    parser.add_argument("--count", type=_positive_int, default=5, help="number of keys (default 5)")
    parser.add_argument(
        "--budget",
        type=_positive_float,
        default=2000.0,
        help="max_budget in USD per key (default 2000)",
    )
    parser.add_argument(
        "--budget-duration",
        default="30d",
        help="budget window per key, e.g. 30d (default 30d)",
    )
    parser.add_argument("--rpm", type=_positive_int, help="rpm_limit per key")
    parser.add_argument("--tpm", type=_positive_int, help="tpm_limit per key")
    parser.add_argument(
        "--max-parallel", type=_positive_int, help="max_parallel_requests per key"
    )
    parser.add_argument("--base-url", default=None, help="defaults to LITELLM_BASE_URL")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"default {DEFAULT_OUTPUT}")
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    parser.add_argument("--timeout", type=_positive_float, default=30.0)
    parser.add_argument(
        "--skip-provider-check",
        action="store_true",
        help="only after completing the documented manual Admin UI verification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate arguments and print the payload shape; no network calls",
    )
    return parser.parse_args(argv)


def key_alias(index: int) -> str:
    return f"customer-{index:02d}"


def build_payload(
    *,
    alias: str,
    budget: float,
    budget_duration: str,
    rpm: int,
    tpm: int,
    max_parallel: int,
    models: list[str] | None = None,
) -> dict[str, Any]:
    """Build a /key/generate body using documented v1.99.0 fields only.

    `models` is this customer's own selection. It defaults to the whole customer
    catalogue via the access group. Either way the key is restricted to what is
    listed, so a model added to config.yaml later reaches nobody automatically.

    Deliberately absent: team_id (keys are standalone), all-proxy-models,
    model_max_budget and budget_fallbacks (a budget-exhausted key must fail,
    never reroute to a cheaper model).
    """

    return {
        "key_alias": alias,
        "models": list(models) if models else [CUSTOMER_ACCESS_GROUP],
        "max_budget": positive_number(budget, name="max_budget"),
        "budget_duration": validate_budget_duration(budget_duration),
        "rpm_limit": positive_number(rpm, name="rpm_limit", integer=True),
        "tpm_limit": positive_number(tpm, name="tpm_limit", integer=True),
        "max_parallel_requests": positive_number(
            max_parallel, name="max_parallel_requests", integer=True
        ),
    }


def preflight(
    target: str, admin_key: str, *, timeout: float, skip_provider_check: bool = False
) -> set[str]:
    """Refuse to issue keys unless the proxy is ready and Azure-only.

    Returns the set of models carrying the customer access group, i.e. the
    catalogue a customer key may be granted from.
    """

    status, _, readiness = http_request("GET", f"{target}/health/readiness", timeout=timeout)
    if status != 200 or not isinstance(readiness, dict) or readiness.get("status") != "healthy":
        raise ContractError(f"/health/readiness is not healthy (HTTP {status})")
    database = str(readiness.get("db", ""))
    if database.lower() != "connected":
        raise ContractError(
            f"/health/readiness reports db={database!r}. Postgres is mandatory: "
            "virtual keys, spend tracking and budget enforcement all need it."
        )
    print("preflight: readiness ok, database connected")

    _, _, models = http_request("GET", f"{target}/v1/models", bearer=admin_key, timeout=timeout)
    names = [name for entry in model_entries(models) if (name := public_model_name(entry))]
    if not names:
        raise ContractError("/v1/models returned no models; nothing to grant")
    absent = missing_aliases(names)
    if absent:
        raise ContractError(
            "/v1/models is missing expected aliases: "
            + ", ".join(absent)
            + f". Available: {', '.join(sorted(names))}"
        )
    print(f"preflight: /v1/models exposes {len(names)} models, every expected alias present")

    if skip_provider_check:
        print("preflight: provider check SKIPPED at operator request")
        return set(names)

    try:
        _, _, info = http_request(
            "GET", f"{target}/model/info", bearer=admin_key, timeout=timeout
        )
    except HttpError as error:
        raise ContractError(f"{MANUAL_PROVIDER_CHECK}\n\n(/model/info returned {error.status})")

    entries = model_entries(info)
    if not entries:
        raise ContractError(MANUAL_PROVIDER_CHECK)

    granted = [
        entry for entry in entries if CUSTOMER_ACCESS_GROUP in access_groups(entry)
    ]
    if not granted:
        raise ContractError(
            f"no configured model carries the {CUSTOMER_ACCESS_GROUP!r} access group, so "
            "keys granted that group could not call anything. Add "
            f'model_info.access_groups: ["{CUSTOMER_ACCESS_GROUP}"] to each verified model.'
        )

    unknown: list[str] = []
    foreign: list[str] = []
    for entry in granted:
        name = public_model_name(entry) or "<unnamed>"
        underlying = underlying_model(entry)
        if underlying is None:
            unknown.append(name)
        elif not is_azure_backed(underlying):
            foreign.append(f"{name} -> {underlying}")

    if unknown:
        raise ContractError(
            MANUAL_PROVIDER_CHECK
            + "\n\n(/model/info did not report litellm_params.model for: "
            + ", ".join(sorted(unknown))
            + ")"
        )
    if foreign:
        raise ContractError(
            f"Refusing to issue keys. These models are in the {CUSTOMER_ACCESS_GROUP!r} "
            "access group but are NOT Azure-backed, so customers would reach a provider "
            "billed outside your Azure invoice:\n  " + "\n  ".join(sorted(foreign))
        )
    ungrouped = len(entries) - len(granted)
    print(
        f"preflight: {len(granted)} model(s) in {CUSTOMER_ACCESS_GROUP!r}, all Azure-backed"
        + (f"; {ungrouped} configured model(s) stay hidden from customer keys" if ungrouped else "")
    )
    return {name for entry in granted if (name := public_model_name(entry))}


def main(argv: list[str] | None = None) -> int:
    register_environment_secrets()
    args = parse_args(argv)

    try:
        if args.profiles:
            profiles = load_profiles(args.profiles)
            payloads = [
                build_payload(
                    alias=profile["key_alias"],
                    models=profile["models"],
                    budget=profile["max_budget"],
                    budget_duration=profile["budget_duration"],
                    rpm=profile["rpm_limit"],
                    tpm=profile["tpm_limit"],
                    max_parallel=profile["max_parallel_requests"],
                )
                for profile in profiles
            ]
        else:
            missing = [
                flag
                for flag, value in (
                    ("--rpm", args.rpm),
                    ("--tpm", args.tpm),
                    ("--max-parallel", args.max_parallel),
                )
                if value is None
            ]
            if missing:
                raise ContractError(
                    "without --profiles these flags are required: "
                    + ", ".join(missing)
                    + ". Use --profiles for per-customer models and budgets."
                )
            duration = validate_budget_duration(args.budget_duration)
            payloads = [
                build_payload(
                    alias=key_alias(index),
                    budget=args.budget,
                    budget_duration=duration,
                    rpm=args.rpm,
                    tpm=args.tpm,
                    max_parallel=args.max_parallel,
                )
                for index in range(1, args.count + 1)
            ]
    except (ContractError, ValueError) as error:
        print(f"error: {redact(error)}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("dry run: no network calls, no keys created")
        for payload in payloads:
            print(
                f"  {payload['key_alias']}: ${payload['max_budget']:g}/"
                f"{payload['budget_duration']} models={payload['models']} "
                f"rpm={payload['rpm_limit']} tpm={payload['tpm_limit']} "
                f"parallel={payload['max_parallel_requests']}"
            )
        return 0

    try:
        target = base_url(args.base_url)
        admin_key = require_env("LITELLM_MASTER_KEY")
        output = Path(args.output)
        if output.exists() and not args.force:
            raise ContractError(
                f"{output} already exists and may hold live customer keys. "
                "Move it into your secret store, or pass --force to overwrite."
            )
        eligible = preflight(
            target,
            admin_key,
            timeout=args.timeout,
            skip_provider_check=args.skip_provider_check,
        )
        check_requested_models(payloads, eligible)
    except ContractError as error:
        print(f"error: {redact(error)}", file=sys.stderr)
        return 2

    created: list[dict[str, Any]] = []
    failure: str | None = None
    for payload in payloads:
        alias = payload["key_alias"]
        try:
            _, _, response = http_request(
                "POST",
                f"{target}/key/generate",
                payload=payload,
                bearer=admin_key,
                timeout=args.timeout,
            )
            key_value = response.get("key") if isinstance(response, dict) else None
            if not isinstance(key_value, str) or not key_value:
                raise ContractError("/key/generate response did not contain a key value")
            created.append(
                {
                    "key_alias": alias,
                    "key": key_value,
                    "models": list(payload["models"]),
                    "max_budget": payload["max_budget"],
                    "budget_duration": payload["budget_duration"],
                    "rpm_limit": payload["rpm_limit"],
                    "tpm_limit": payload["tpm_limit"],
                    "max_parallel_requests": payload["max_parallel_requests"],
                }
            )
            print(f"created: {alias} {mask_key(key_value)}")
        except (HttpError, ContractError) as error:
            failure = f"{alias}: {redact(error)}"
            print(f"error: {failure}", file=sys.stderr)
            break

    if created:
        try:
            written = write_secret_json(output, created, force=True)
        except OSError as error:
            print(f"error: could not write {output}: {redact(error)}", file=sys.stderr)
            print(
                "The keys below were created on the proxy but NOT saved. "
                "Revoke them with POST /key/block and re-run.",
                file=sys.stderr,
            )
            for item in created:
                print(f"  {item['key_alias']}", file=sys.stderr)
            return 1

        print(f"\nwrote {len(created)} key(s) to {written} with owner-only permissions")
        print(
            "WARNING: a virtual key value may only be shown once. This file is the "
            "only copy.\n"
            "         Move it into your approved secret store, hand each customer only "
            "their own key,\n"
            "         then delete the local file. It is git-ignored - never commit it."
        )

    if failure:
        print(f"\nkey creation stopped after {len(created)} key(s): {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
