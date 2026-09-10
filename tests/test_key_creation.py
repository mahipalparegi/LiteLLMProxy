"""Unit tests for key creation, validation and secret hygiene.

Every HTTP call is mocked. No real model call, no real proxy, no real Azure.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import common, concurrency_check, create_virtual_keys, smoke_test

MASTER_KEY = "sk-master-do-not-leak-0123456789"


def _payload() -> dict:
    return create_virtual_keys.build_payload(
        alias="customer-01",
        budget=2000.0,
        budget_duration="30d",
        rpm=60,
        tpm=100_000,
        max_parallel=4,
    )


def test_payload_grants_the_access_group_not_every_proxy_model() -> None:
    payload = _payload()
    assert payload["models"] == [common.CUSTOMER_ACCESS_GROUP]
    assert common.ALL_PROXY_MODELS not in payload["models"]
    assert payload["key_alias"] == "customer-01"
    assert payload["max_budget"] == 2000.0
    assert payload["budget_duration"] == "30d"
    assert payload["rpm_limit"] == 60
    assert payload["tpm_limit"] == 100_000
    assert payload["max_parallel_requests"] == 4


def test_payload_is_standalone_and_has_no_budget_reroute_fields() -> None:
    payload = _payload()
    for forbidden in ("team_id", "model_max_budget", "budget_fallbacks", "aliases"):
        assert forbidden not in payload


def test_payload_limits_are_integers_and_budget_is_numeric() -> None:
    payload = _payload()
    assert isinstance(payload["rpm_limit"], int)
    assert isinstance(payload["tpm_limit"], int)
    assert isinstance(payload["max_parallel_requests"], int)
    assert isinstance(payload["max_budget"], float)


def test_key_aliases_are_zero_padded_and_sequential() -> None:
    assert [create_virtual_keys.key_alias(i) for i in range(1, 6)] == [
        "customer-01",
        "customer-02",
        "customer-03",
        "customer-04",
        "customer-05",
    ]


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", 0, 0.0, -1, -0.5, "0", "-3", "abc", "1e", True, False, float("nan"), math.inf, -math.inf],
)
def test_positive_number_rejects_bad_values(value) -> None:
    with pytest.raises(ValueError):
        common.positive_number(value, name="rpm_limit")


@pytest.mark.parametrize("value", [1, 2.5, "7", " 12 ", 1e6])
def test_positive_number_accepts_positive_finite_values(value) -> None:
    assert common.positive_number(value, name="max_budget") > 0


def test_positive_number_integer_mode_rejects_fractions() -> None:
    with pytest.raises(ValueError):
        common.positive_number(1.5, name="rpm_limit", integer=True)
    assert common.positive_number("60", name="rpm_limit", integer=True) == 60


@pytest.mark.parametrize("value", ["30d", "24h", "30m", "30s", "1d"])
def test_budget_duration_accepts_documented_forms(value) -> None:
    assert create_virtual_keys.validate_budget_duration(value) == value


@pytest.mark.parametrize("value", ["", "30", "d30", "0d", "-1d", "30 d", "30w", "thirty-days"])
def test_budget_duration_rejects_anything_else(value) -> None:
    with pytest.raises(ValueError):
        create_virtual_keys.validate_budget_duration(value)


@pytest.mark.parametrize(
    "argv",
    [
        ["--rpm", "0", "--tpm", "10", "--max-parallel", "1"],
        ["--rpm", "10", "--tpm", "-5", "--max-parallel", "1"],
        ["--rpm", "10", "--tpm", "10", "--max-parallel", "nan"],
        ["--rpm", "10", "--tpm", "10", "--max-parallel", "1", "--budget", "0"],
        ["--rpm", "10", "--tpm", "10", "--max-parallel", "1", "--count", "0"],
    ],
)
def test_cli_rejects_missing_zero_negative_and_nan_limits(argv) -> None:
    with pytest.raises(SystemExit):
        create_virtual_keys.parse_args(argv)


def test_redact_removes_registered_secret_and_any_sk_token() -> None:
    common.register_secret(MASTER_KEY)
    text = f"auth failed for {MASTER_KEY} and sk-another-key-value-999"
    scrubbed = common.redact(text)
    assert MASTER_KEY not in scrubbed
    assert "sk-another-key-value-999" not in scrubbed
    assert common.REDACTED in scrubbed


def test_redact_removes_credentials_embedded_in_a_url() -> None:
    # Assembled from parts so this fixture is not itself a credential-shaped
    # literal that the CI secret scan would flag.
    password = "EXAMPLE_ONLY_pw"
    url = "postgresql://litellm:" + password + "@db.internal:5432/litellm"
    scrubbed = common.redact(url)
    assert password not in scrubbed
    assert common.REDACTED in scrubbed


def test_http_error_text_never_contains_the_master_key() -> None:
    common.register_secret(MASTER_KEY)
    error = common.HttpError(401, f"https://proxy/key/generate?k={MASTER_KEY}", f"bad {MASTER_KEY}")
    assert MASTER_KEY not in str(error)
    assert MASTER_KEY not in error.body
    assert MASTER_KEY not in error.url


def test_mask_key_never_returns_the_key() -> None:
    masked = common.mask_key(MASTER_KEY)
    assert MASTER_KEY not in masked
    assert masked.endswith("6789>")


def test_common_exposes_only_the_helpers_the_scripts_use() -> None:
    """Guards against dead or duplicated helpers creeping back into common.py."""

    public = {name for name in vars(common) if not name.startswith("_")}
    helpers = {
        name
        for name in public
        if callable(getattr(common, name)) and getattr(common, name).__module__ == common.__name__
    }
    assert helpers == {
        "register_secret",
        "register_environment_secrets",
        "redact",
        "mask_key",
        "HttpError",
        "ContractError",
        "require_env",
        "base_url",
        "positive_number",
        "http_request",
        "try_http_request",
        "decode_json",
        "model_entries",
        "public_model_name",
        "underlying_model",
        "access_groups",
        "is_azure_backed",
        "token_limit_param",
        "missing_aliases",
        "write_secret_json",
        "file_mode",
    }


def test_write_secret_json_restricts_permissions(tmp_path: Path) -> None:
    target = tmp_path / "generated-keys.json"
    common.write_secret_json(target, [{"key_alias": "customer-01"}])
    assert json.loads(target.read_text(encoding="utf-8"))[0]["key_alias"] == "customer-01"
    if os.name == "posix":
        assert common.file_mode(target) == 0o600


def test_write_secret_json_refuses_to_clobber_without_force(tmp_path: Path) -> None:
    target = tmp_path / "generated-keys.json"
    target.write_text("[]", encoding="utf-8")
    with pytest.raises(common.ContractError):
        common.write_secret_json(target, [{"key_alias": "customer-01"}])
    common.write_secret_json(target, [{"key_alias": "customer-02"}], force=True)
    assert json.loads(target.read_text(encoding="utf-8"))[0]["key_alias"] == "customer-02"


BASE_ARGS = [
    "--base-url",
    "https://proxy.example.com",
    "--rpm",
    "60",
    "--tpm",
    "100000",
    "--max-parallel",
    "4",
]


def test_main_creates_requested_keys_and_writes_the_output_file(tmp_path: Path, capsys) -> None:
    output = tmp_path / "generated-keys.json"
    sent: list[dict] = []

    def fake_http(method, url, *, payload=None, bearer=None, headers=None, timeout=30.0):
        assert method == "POST"
        assert url.endswith("/key/generate")
        sent.append(payload)
        return 200, {}, {"key": f"sk-generated-{len(sent):02d}"}

    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": MASTER_KEY}, clear=False):
        with patch.object(create_virtual_keys, "preflight") as preflight:
            with patch.object(create_virtual_keys, "http_request", side_effect=fake_http):
                exit_code = create_virtual_keys.main(
                    [*BASE_ARGS, "--count", "5", "--output", str(output)]
                )

    assert exit_code == 0
    preflight.assert_called_once()
    assert len(sent) == 5
    assert {item["key_alias"] for item in sent} == {
        "customer-01",
        "customer-02",
        "customer-03",
        "customer-04",
        "customer-05",
    }
    assert all(item["models"] == [common.CUSTOMER_ACCESS_GROUP] for item in sent)

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert [row["key_alias"] for row in saved] == [f"customer-0{i}" for i in range(1, 6)]
    assert saved[0]["max_budget"] == 2000.0
    assert saved[0]["budget_duration"] == "30d"

    stdout = capsys.readouterr().out
    assert MASTER_KEY not in stdout
    assert "sk-generated-01" not in stdout  # only masked hints are printed


def test_main_fails_clearly_on_non_2xx_without_leaking_the_master_key(
    tmp_path: Path, capsys
) -> None:
    output = tmp_path / "generated-keys.json"

    def failing_http(*_args, **_kwargs):
        raise common.HttpError(
            401, "https://proxy.example.com/key/generate", f"invalid admin key {MASTER_KEY}"
        )

    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": MASTER_KEY}, clear=False):
        with patch.object(create_virtual_keys, "preflight"):
            with patch.object(create_virtual_keys, "http_request", side_effect=failing_http):
                exit_code = create_virtual_keys.main([*BASE_ARGS, "--output", str(output)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "HTTP 401" in captured.err
    assert MASTER_KEY not in captured.err
    assert MASTER_KEY not in captured.out
    assert not output.exists()


def test_main_refuses_to_overwrite_an_existing_output_file(tmp_path: Path, capsys) -> None:
    output = tmp_path / "generated-keys.json"
    output.write_text('[{"key_alias": "customer-01"}]', encoding="utf-8")

    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": MASTER_KEY}, clear=False):
        with patch.object(create_virtual_keys, "preflight") as preflight:
            with patch.object(create_virtual_keys, "http_request") as request:
                exit_code = create_virtual_keys.main([*BASE_ARGS, "--output", str(output)])

    assert exit_code == 2
    preflight.assert_not_called()
    request.assert_not_called()
    assert "already exists" in capsys.readouterr().err


def test_dry_run_makes_no_network_calls(capsys) -> None:
    with patch.object(create_virtual_keys, "http_request") as request:
        exit_code = create_virtual_keys.main([*BASE_ARGS, "--dry-run"])
    assert exit_code == 0
    request.assert_not_called()
    assert "dry run" in capsys.readouterr().out


READY = (200, {}, {"status": "healthy", "db": "connected"})


def _preflight_responses(model_info):
    def fake_http(method, url, *, payload=None, bearer=None, headers=None, timeout=30.0):
        if url.endswith("/health/readiness"):
            return READY
        if url.endswith("/v1/models"):
            return 200, {}, {"data": [{"id": alias} for alias in common.PROXY_ALIASES]}
        if url.endswith("/model/info"):
            return 200, {}, model_info
        raise AssertionError(f"unexpected call to {url}")

    return fake_http


GRANTED = {"access_groups": [common.CUSTOMER_ACCESS_GROUP]}


def _azure_entry(alias: str, model_info: dict | None = None) -> dict:
    prefix = "azure/" if alias in common.OPENAI_ALIASES else "azure_ai/"
    return {
        "model_name": alias,
        "litellm_params": {"model": f"{prefix}{alias}"},
        "model_info": dict(model_info if model_info is not None else GRANTED),
    }


def test_preflight_passes_when_every_granted_model_is_azure_backed(capsys) -> None:
    info = {"data": [_azure_entry(alias) for alias in common.PROXY_ALIASES]}
    with patch.object(create_virtual_keys, "http_request", side_effect=_preflight_responses(info)):
        create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    output = capsys.readouterr().out
    assert "every expected alias present" in output
    assert f"{len(common.PROXY_ALIASES)} model(s) in 'customer-models'" in output


def test_preflight_blocks_a_non_azure_model_inside_the_access_group() -> None:
    info = {
        "data": [
            *[_azure_entry(alias) for alias in common.PROXY_ALIASES],
            {
                "model_name": "gpt-5-direct",
                "litellm_params": {"model": "openai/gpt-5"},
                "model_info": dict(GRANTED),
            },
        ]
    }
    with patch.object(create_virtual_keys, "http_request", side_effect=_preflight_responses(info)):
        with pytest.raises(common.ContractError) as error:
            create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    assert "not azure-backed" in str(error.value).lower()
    assert "openai/gpt-5" in str(error.value)


def test_preflight_allows_a_non_azure_model_outside_the_access_group(capsys) -> None:
    """The access group, not the model list, is the customer boundary."""

    info = {
        "data": [
            *[_azure_entry(alias) for alias in common.PROXY_ALIASES],
            {
                "model_name": "gpt-5-direct",
                "litellm_params": {"model": "openai/gpt-5"},
                "model_info": {"access_groups": ["admin-only"]},
            },
        ]
    }
    with patch.object(create_virtual_keys, "http_request", side_effect=_preflight_responses(info)):
        create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    assert "1 configured model(s) stay hidden" in capsys.readouterr().out


def test_preflight_stops_when_no_model_carries_the_access_group() -> None:
    info = {"data": [_azure_entry(alias, {}) for alias in common.PROXY_ALIASES]}
    with patch.object(create_virtual_keys, "http_request", side_effect=_preflight_responses(info)):
        with pytest.raises(common.ContractError) as error:
            create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    assert "access_groups" in str(error.value)


def test_preflight_gives_manual_steps_when_provider_cannot_be_confirmed() -> None:
    info = {
        "data": [
            {"model_name": alias, "model_info": dict(GRANTED)}
            for alias in common.PROXY_ALIASES
        ]
    }
    with patch.object(create_virtual_keys, "http_request", side_effect=_preflight_responses(info)):
        with pytest.raises(common.ContractError) as error:
            create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    message = str(error.value)
    assert "Admin UI" in message
    assert "Models + Endpoints" in message


def test_preflight_requires_a_connected_database() -> None:
    def fake_http(method, url, **_kwargs):
        if url.endswith("/health/readiness"):
            return 200, {}, {"status": "healthy", "db": "disconnected"}
        raise AssertionError("should not get past readiness")

    with patch.object(create_virtual_keys, "http_request", side_effect=fake_http):
        with pytest.raises(common.ContractError) as error:
            create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    assert "Postgres is mandatory" in str(error.value)


def test_preflight_requires_a_non_empty_model_list() -> None:
    def fake_http(method, url, **_kwargs):
        if url.endswith("/health/readiness"):
            return READY
        if url.endswith("/v1/models"):
            return 200, {}, {"data": []}
        raise AssertionError("should not reach /model/info")

    with patch.object(create_virtual_keys, "http_request", side_effect=fake_http):
        with pytest.raises(common.ContractError) as error:
            create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    assert "no models" in str(error.value)


def test_preflight_requires_every_customer_alias() -> None:
    def fake_http(method, url, **_kwargs):
        if url.endswith("/health/readiness"):
            return READY
        if url.endswith("/v1/models"):
            return 200, {}, {"data": [{"id": "gpt-5.6-sol"}]}
        raise AssertionError("should not reach /model/info")

    with patch.object(create_virtual_keys, "http_request", side_effect=fake_http):
        with pytest.raises(common.ContractError) as error:
            create_virtual_keys.preflight("https://proxy.example.com", MASTER_KEY, timeout=5)
    assert "gpt-5.5" in str(error.value)


def test_management_route_accepted_is_reported_as_a_failure() -> None:
    report = smoke_test.Report()
    with patch.object(smoke_test, "try_http_request", return_value=(200, {}, {})):
        smoke_test.check_forbidden_routes("https://proxy.example.com", "sk-customer-key-1", report, 5)
    assert report.failures
    assert all("ACCEPTED" in failure for failure in report.failures)


def test_management_route_denied_is_reported_as_a_pass() -> None:
    report = smoke_test.Report()
    with patch.object(smoke_test, "try_http_request", return_value=(403, {}, {})):
        smoke_test.check_forbidden_routes("https://proxy.example.com", "sk-customer-key-1", report, 5)
    assert not report.failures
    assert not report.warnings


def test_management_route_missing_is_a_warning_not_a_pass() -> None:
    report = smoke_test.Report()
    with patch.object(smoke_test, "try_http_request", return_value=(404, {}, {})):
        smoke_test.check_forbidden_routes("https://proxy.example.com", "sk-customer-key-1", report, 5)
    assert not report.failures
    assert len(report.warnings) == len(smoke_test.FORBIDDEN_ROUTES)


def test_smoke_test_never_prints_the_key_it_uses(capsys) -> None:
    customer_key = "sk-customer-abcdefghijklmnop"
    with patch.dict(
        os.environ,
        {"LITELLM_BASE_URL": "https://proxy.example.com", "LITELLM_API_KEY": customer_key},
        clear=False,
    ):
        with patch.object(smoke_test, "try_http_request", return_value=(503, {}, {})):
            with patch.object(
                smoke_test, "http_request", side_effect=common.HttpError(503, "u", "down")
            ):
                smoke_test.main(["--skip-generation"])
    captured = capsys.readouterr()
    assert customer_key not in captured.out
    assert customer_key not in captured.err


def test_concurrency_check_defaults_to_twenty_free_requests() -> None:
    args = concurrency_check.parse_args([])
    assert args.concurrency == 20
    assert args.generate is False
    assert args.confirmed is False


@pytest.mark.parametrize("value", ["0", "-4", "nan", "abc", "2.5"])
def test_concurrency_check_rejects_invalid_targets(value) -> None:
    with pytest.raises(SystemExit):
        concurrency_check.parse_args(["--concurrency", value])


def test_concurrency_check_refuses_billable_mode_without_confirmation(capsys) -> None:
    with patch.dict(os.environ, {"LITELLM_BASE_URL": "https://proxy.example.com"}, clear=False):
        with patch.object(concurrency_check, "try_http_request") as request:
            exit_code = concurrency_check.main(["--generate"])
    assert exit_code == 2
    request.assert_not_called()
    assert "spends-money" in capsys.readouterr().err


def test_concurrency_check_refuses_billable_mode_in_ci(capsys) -> None:
    with patch.dict(
        os.environ,
        {"GITHUB_ACTIONS": "true", "LITELLM_BASE_URL": "https://proxy.example.com"},
        clear=False,
    ):
        with patch.object(concurrency_check, "try_http_request") as request:
            exit_code = concurrency_check.main(
                ["--generate", "--i-understand-this-spends-money"]
            )
    assert exit_code == 2
    request.assert_not_called()
    assert "GITHUB_ACTIONS" in capsys.readouterr().err


def test_concurrency_check_passes_when_every_caller_is_served(capsys) -> None:
    env = {
        "LITELLM_BASE_URL": "https://proxy.example.com",
        "LITELLM_API_KEY": "sk-customer-abcdefghijkl",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch.object(concurrency_check, "try_http_request", return_value=(200, {}, {})):
            exit_code = concurrency_check.main(["--concurrency", "20", "--rounds", "1"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "served 20/20" in output
    assert env["LITELLM_API_KEY"] not in output


def test_concurrency_check_fails_on_throttling(capsys) -> None:
    env = {
        "LITELLM_BASE_URL": "https://proxy.example.com",
        "LITELLM_API_KEY": "sk-customer-abcdefghijkl",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch.object(concurrency_check, "try_http_request", return_value=(429, {}, {})):
            exit_code = concurrency_check.main(["--concurrency", "4", "--rounds", "1"])
    assert exit_code == 1
    assert "throttled" in capsys.readouterr().out


def _profile(**overrides) -> dict:
    base = {
        "key_alias": "acme-corp",
        "models": ["gpt-5.6-sol", "gpt-5.6-luna"],
        "max_budget": 1000,
        "budget_duration": "30d",
        "rpm_limit": 300,
        "tpm_limit": 1000000,
        "max_parallel_requests": 15,
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_profile_gives_a_customer_only_their_selected_models(tmp_path: Path) -> None:
    profiles = create_virtual_keys.load_profiles(_write(tmp_path, [_profile()]))
    payload = create_virtual_keys.build_payload(
        alias=profiles[0]["key_alias"],
        models=profiles[0]["models"],
        budget=profiles[0]["max_budget"],
        budget_duration=profiles[0]["budget_duration"],
        rpm=profiles[0]["rpm_limit"],
        tpm=profiles[0]["tpm_limit"],
        max_parallel=profiles[0]["max_parallel_requests"],
    )
    assert payload["models"] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    assert payload["max_budget"] == 1000
    assert common.CUSTOMER_ACCESS_GROUP not in payload["models"]
    assert common.ALL_PROXY_MODELS not in payload["models"]


def test_each_profile_keeps_its_own_budget(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            _profile(key_alias="small", max_budget=500),
            _profile(key_alias="large", max_budget=4000),
        ],
    )
    budgets = {p["key_alias"]: p["max_budget"] for p in create_virtual_keys.load_profiles(path)}
    assert budgets == {"small": 500.0, "large": 4000.0}


def test_profile_rejects_a_misspelled_limit_instead_of_ignoring_it(tmp_path: Path) -> None:
    """A silently ignored max_budgett would create a key with no budget at all."""

    broken = _profile()
    broken["max_budgett"] = broken.pop("max_budget")
    with pytest.raises(common.ContractError) as error:
        create_virtual_keys.load_profiles(_write(tmp_path, [broken]))
    assert "unrecognised field" in str(error.value)
    assert "max_budgett" in str(error.value)


@pytest.mark.parametrize(
    "override",
    [
        {"max_budget": 0},
        {"max_budget": -100},
        {"max_budget": "nan"},
        {"rpm_limit": 0},
        {"tpm_limit": -1},
        {"max_parallel_requests": 0},
        {"budget_duration": "1 month"},
        {"budget_duration": ""},
        {"models": []},
        {"models": ["gpt-5.6-sol", "gpt-5.6-sol"]},
        {"models": [""]},
        {"key_alias": ""},
        {"key_alias": "a b"},
    ],
)
def test_profile_validation_rejects_bad_values(tmp_path: Path, override) -> None:
    with pytest.raises(common.ContractError):
        create_virtual_keys.load_profiles(_write(tmp_path, [_profile(**override)]))


def test_profile_rejects_duplicate_aliases(tmp_path: Path) -> None:
    path = _write(tmp_path, [_profile(), _profile()])
    with pytest.raises(common.ContractError) as error:
        create_virtual_keys.load_profiles(path)
    assert "duplicates key_alias" in str(error.value)


def test_profile_requires_a_non_empty_array(tmp_path: Path) -> None:
    with pytest.raises(common.ContractError):
        create_virtual_keys.load_profiles(_write(tmp_path, []))


def test_requested_models_must_be_in_the_approved_catalogue() -> None:
    payloads = [{"key_alias": "acme", "models": ["gpt-5.5", "this-model-is-not-configured"]}]
    with pytest.raises(common.ContractError) as error:
        create_virtual_keys.check_requested_models(payloads, {"gpt-5.5", "gpt-5.6-sol"})
    message = str(error.value)
    assert "acme -> this-model-is-not-configured" in message
    assert common.CUSTOMER_ACCESS_GROUP in message


def test_requested_models_accept_the_catalogue_group_itself() -> None:
    payloads = [{"key_alias": "hooli", "models": [common.CUSTOMER_ACCESS_GROUP]}]
    create_virtual_keys.check_requested_models(payloads, {"gpt-5.6-sol"})


def test_shipped_example_profiles_only_request_customer_catalogue_models() -> None:
    path = Path(__file__).resolve().parents[1] / "customer-profiles.example.json"
    profiles = create_virtual_keys.load_profiles(path)
    assert len(profiles) >= 2
    payloads = [{"key_alias": p["key_alias"], "models": p["models"]} for p in profiles]
    create_virtual_keys.check_requested_models(payloads, set(common.PROXY_ALIASES))
    assert {p["max_budget"] for p in profiles} != {profiles[0]["max_budget"]}


def test_flag_mode_still_requires_the_three_limits(capsys) -> None:
    exit_code = create_virtual_keys.main(["--base-url", "https://proxy.example.com", "--dry-run"])
    assert exit_code == 2
    error = capsys.readouterr().err
    assert "--rpm" in error and "--tpm" in error and "--max-parallel" in error


def test_profile_mode_creates_one_key_per_customer(tmp_path: Path, capsys) -> None:
    path = _write(
        tmp_path,
        [_profile(key_alias="acme", max_budget=1000), _profile(key_alias="globex", max_budget=500)],
    )
    output = tmp_path / "generated-keys.json"
    sent: list[dict] = []

    def fake_http(method, url, *, payload=None, bearer=None, headers=None, timeout=30.0):
        sent.append(payload)
        return 200, {}, {"key": f"sk-generated-{len(sent):02d}"}

    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": MASTER_KEY}, clear=False):
        with patch.object(
            create_virtual_keys, "preflight", return_value=set(common.PROXY_ALIASES)
        ):
            with patch.object(create_virtual_keys, "http_request", side_effect=fake_http):
                exit_code = create_virtual_keys.main(
                    [
                        "--profiles",
                        str(path),
                        "--output",
                        str(output),
                        "--base-url",
                        "https://proxy.example.com",
                    ]
                )

    assert exit_code == 0
    assert [item["key_alias"] for item in sent] == ["acme", "globex"]
    assert [item["max_budget"] for item in sent] == [1000.0, 500.0]
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved[0]["models"] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    assert MASTER_KEY not in capsys.readouterr().out


def test_profile_mode_refuses_a_model_outside_the_catalogue(tmp_path: Path, capsys) -> None:
    path = _write(tmp_path, [_profile(models=["this-model-is-not-configured"])])
    output = tmp_path / "generated-keys.json"

    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": MASTER_KEY}, clear=False):
        with patch.object(
            create_virtual_keys, "preflight", return_value={"gpt-5.6-sol"}
        ):
            with patch.object(create_virtual_keys, "http_request") as request:
                exit_code = create_virtual_keys.main(
                    [
                        "--profiles",
                        str(path),
                        "--output",
                        str(output),
                        "--base-url",
                        "https://proxy.example.com",
                    ]
                )

    assert exit_code == 2
    request.assert_not_called()
    assert not output.exists()
    assert "this-model-is-not-configured" in capsys.readouterr().err
