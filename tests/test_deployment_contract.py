"""Static contract tests for the deployment files.

No network access, no Docker, no proxy. These only read files in the repo.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.common import (
    ALL_ALIASES,
    ALL_PROXY_MODELS,
    AZURE_PREFIXES,
    CUSTOMER_ACCESS_GROUP,
    OPENAI_ALIASES,
    PREVIEW_ACCESS_GROUP,
    PREVIEW_ALIASES,
    PROXY_ALIASES,
)

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to validate the YAML contracts")

ROOT = Path(__file__).resolve().parents[1]
PINNED_IMAGE = "ghcr.io/berriai/litellm-database:${LITELLM_VERSION}"
POSTGRES_CONNECTION_CAP = 400
SECRET_ENV_KEYS = (
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "AZURE_OPENAI_API_BASE",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
)


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def load_yaml(name: str) -> dict:
    return yaml.safe_load(read(name))


def _instructions(name: str) -> list[str]:
    return [
        line.strip()
        for line in read(name).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerfile_pins_the_official_image() -> None:
    dockerfile = read("Dockerfile")
    assert "ARG LITELLM_VERSION=v1.99.0" in dockerfile
    assert f"FROM {PINNED_IMAGE}" in dockerfile
    for line in _instructions("Dockerfile"):
        assert "litellm-database:latest" not in line, line
        assert "main-stable" not in line, line


def test_dockerfile_copies_only_config_and_start_script() -> None:
    copies = [line for line in read("Dockerfile").splitlines() if line.startswith("COPY ")]
    assert copies == [
        "COPY config.yaml /app/config.yaml",
        "COPY scripts/render_start.sh /app/scripts/render_start.sh",
    ]


def test_dockerfile_exposes_4000_and_uses_the_startup_contract() -> None:
    dockerfile = read("Dockerfile")
    assert "EXPOSE 4000" in dockerfile
    assert 'ENTRYPOINT ["/bin/sh", "/app/scripts/render_start.sh"]' in dockerfile


def test_dockerfile_does_not_invent_a_non_root_user() -> None:
    lines = [line.strip() for line in read("Dockerfile").splitlines()]
    assert not any(line.startswith("USER ") for line in lines)
    assert "litellm-non_root" in read("Dockerfile")


def test_dockerfile_installs_nothing_and_takes_no_secret_build_args() -> None:
    dockerfile = read("Dockerfile")
    assert "RUN " not in dockerfile
    assert "apt-get" not in dockerfile and "pip install" not in dockerfile
    args = [line for line in dockerfile.splitlines() if line.startswith(("ARG ", "ENV "))]
    assert args == ["ARG LITELLM_VERSION=v1.99.0"]


def test_start_script_is_posix_and_strict() -> None:
    script = read("scripts/render_start.sh")
    assert script.startswith("#!/bin/sh")
    assert "set -eu" in script


def test_start_script_requires_every_mandatory_variable() -> None:
    script = read("scripts/render_start.sh")
    for name in (
        "DATABASE_URL",
        "LITELLM_MASTER_KEY",
        "LITELLM_SALT_KEY",
        "AZURE_OPENAI_API_BASE",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
    ):
        assert name in script
    assert "must start with sk-" in script
    assert "must be different values" in script


def test_start_script_requires_no_api_version_and_no_anthropic_endpoint() -> None:
    """Both were removed: v1.99.0 defaults the api-version, and no Claude model
    remains, so a second Foundry endpoint would be dead configuration."""

    script = read("scripts/render_start.sh")
    assert "AZURE_OPENAI_API_VERSION" not in script
    assert "AZURE_API_BASE" not in script
    assert "anthropic" not in script.lower()


def test_start_script_normalises_the_portal_azure_openai_endpoint() -> None:
    """The portal shows the v1 surface; the azure/ route needs the resource root."""

    script = read("scripts/render_start.sh")
    assert "%/openai/v1}" in script
    assert "%/}" in script
    assert "export AZURE_OPENAI_API_BASE" in script
    assert "/api/projects/" in script


def test_start_script_validates_an_api_version_override_when_present() -> None:
    script = read("scripts/render_start.sh")
    assert "AZURE_API_VERSION-" in script
    assert "????-??-??" in script


def test_start_script_treats_azure_scope_as_optional() -> None:
    script = read("scripts/render_start.sh")
    assert "AZURE_SCOPE-" in script
    assert "https://cognitiveservices.azure.com/.default" in script


def test_start_script_execs_the_cli_with_the_render_port() -> None:
    script = read("scripts/render_start.sh")
    assert "exec litellm" in script
    assert "--config /app/config.yaml" in script
    assert "--host 0.0.0.0" in script
    assert '--port "${PORT:-4000}"' in script
    assert '--num_workers "${WORKERS}"' in script


def test_start_script_refuses_multiple_workers_without_shared_redis() -> None:
    """Without Redis each worker keeps its own counters, so limits multiply."""

    script = read("scripts/render_start.sh")
    assert '[ "${WORKERS}" -gt 1 ]' in script
    assert "REDIS_URL" in script
    assert "requires REDIS_URL or REDIS_HOST" in script


def test_start_script_never_prints_a_secret_value() -> None:
    printing = [
        line
        for line in read("scripts/render_start.sh").splitlines()
        if re.search(r"\b(printf|echo|log)\b", line) and not line.strip().startswith("#")
    ]
    for line in printing:
        for name in (
            "LITELLM_MASTER_KEY",
            "LITELLM_SALT_KEY",
            "AZURE_CLIENT_SECRET",
            "DATABASE_URL",
            "REDIS_URL",
        ):
            assert f"${{{name}}}" not in line, line
            assert f"${name}" not in line, line


def models() -> list[dict]:
    return load_yaml("config.yaml")["model_list"]


def test_config_lists_every_alias_in_order() -> None:
    assert [entry["model_name"] for entry in models()] == list(ALL_ALIASES)


def test_every_model_is_an_azure_openai_deployment_on_one_endpoint() -> None:
    by_name = {entry["model_name"]: entry for entry in models()}
    assert set(by_name) == set(OPENAI_ALIASES + PREVIEW_ALIASES)
    for alias in OPENAI_ALIASES + PREVIEW_ALIASES:
        params = by_name[alias]["litellm_params"]
        assert params["model"] == f"azure/{alias}"
        assert params["api_base"] == "os.environ/AZURE_OPENAI_API_BASE"


def test_public_alias_and_azure_deployment_name_are_identical() -> None:
    """What a caller asks for is what Azure is asked for. gpt-6-astra reaches the
    gpt-6-astra deployment and nothing else."""

    for entry in models():
        alias = entry["model_name"]
        assert entry["litellm_params"]["model"] == f"azure/{alias}", alias


def test_no_alias_is_declared_twice() -> None:
    """Two entries sharing a model_name make LiteLLM load-balance between them, so
    a caller could silently land on a different deployment."""

    names = [entry["model_name"] for entry in models()]
    assert len(names) == len(set(names))


def test_config_has_no_mechanism_that_could_swap_the_model() -> None:
    rendered = yaml.safe_dump(load_yaml("config.yaml"))
    for forbidden in (
        "model_group_alias",
        "model_alias_map",
        "azure/*",
        "openai/*",
        "wildcard",
        "routing_strategy",
        "model_group_retry_policy",
    ):
        assert forbidden not in rendered, forbidden


def test_config_pins_no_api_version_so_the_image_default_applies() -> None:
    """LiteLLM v1.99.0 defaults to 2025-02-01-preview; AZURE_API_VERSION overrides."""

    rendered = yaml.safe_dump(load_yaml("config.yaml"))
    assert "api_version" not in rendered
    assert "2025-02-01-preview" in read("config.yaml")


def test_no_claude_or_foundry_anthropic_reference_survives_anywhere() -> None:
    """Azure did not offer Claude on this resource, so it was removed outright."""

    for name in (
        "config.yaml",
        "render.yaml",
        ".env.example",
        "scripts/common.py",
        "scripts/smoke_test.py",
        "scripts/test_limits.py",
        "scripts/concurrency_check.py",
        "scripts/create_virtual_keys.py",
        "scripts/verify_models_and_costs.py",
        "scripts/render_start.sh",
        "customer-profiles.example.json",
        "SECURITY.md",
    ):
        text = read(name).lower()
        assert "claude" not in text, name
        assert "anthropic" not in text, name


def test_every_model_is_azure_backed() -> None:
    for entry in models():
        assert entry["litellm_params"]["model"].startswith(AZURE_PREFIXES)


def test_config_reads_every_credential_from_the_environment() -> None:
    for entry in models():
        params = entry["litellm_params"]
        assert params["tenant_id"] == "os.environ/AZURE_TENANT_ID"
        assert params["client_id"] == "os.environ/AZURE_CLIENT_ID"
        assert params["client_secret"] == "os.environ/AZURE_CLIENT_SECRET"
        assert params["azure_scope"] == "os.environ/AZURE_SCOPE"
        assert "api_key" not in params


def test_config_contains_no_inline_credential_and_no_invented_price() -> None:
    rendered = yaml.safe_dump(load_yaml("config.yaml"))
    assert "sk-" not in rendered
    assert "azure.com" not in rendered  # only env var references are allowed
    assert "input_cost_per_token" not in rendered
    assert "output_cost_per_token" not in rendered


def test_access_group_is_the_customer_boundary() -> None:
    """A model outside the group is unreachable by customer keys. That is the point."""

    by_name = {entry["model_name"]: entry for entry in models()}
    for alias in PROXY_ALIASES:
        assert by_name[alias]["model_info"]["access_groups"] == [CUSTOMER_ACCESS_GROUP]
    for alias in PREVIEW_ALIASES:
        groups = by_name[alias]["model_info"]["access_groups"]
        assert CUSTOMER_ACCESS_GROUP not in groups, alias
        assert groups == [PREVIEW_ACCESS_GROUP]


def test_gpt6_stays_quarantined_while_the_image_is_pinned_to_v1_99_0() -> None:
    """v1.99.0 rewrites max_tokens for gpt-5* only, so a gpt-6 caller sending
    max_tokens is rejected by the provider. Promote it after upgrading."""

    dockerfile = read("Dockerfile")
    config = read("config.yaml")
    if "ARG LITELLM_VERSION=v1.99.0" in dockerfile:
        assert "gpt-6-astra" in PREVIEW_ALIASES
        assert "max_completion_tokens" in config or "QUARANTINED" in config


def test_model_list_entries_carry_no_extra_keys() -> None:
    for entry in models():
        assert set(entry) == {"model_name", "litellm_params", "model_info"}
        assert set(entry["model_info"]) == {"mode", "access_groups"}
        assert entry["model_info"]["mode"] == "chat"
        assert set(entry["litellm_params"]) == {
            "model",
            "api_base",
            "tenant_id",
            "client_id",
            "client_secret",
            "azure_scope",
        }


def test_general_settings_enforce_budgets_fail_closed() -> None:
    general = load_yaml("config.yaml")["general_settings"]
    assert general["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert general["database_url"] == "os.environ/DATABASE_URL"
    assert general["store_model_in_db"] is True
    assert general["fail_closed_budget_enforcement"] is True
    assert general["disable_budget_reservation"] is False


def test_config_sections_hold_exactly_the_reviewed_settings() -> None:
    config = load_yaml("config.yaml")
    assert set(config) == {"model_list", "general_settings", "litellm_settings"}
    assert set(config["general_settings"]) == {
        "master_key",
        "database_url",
        "store_model_in_db",
        "fail_closed_budget_enforcement",
        "disable_budget_reservation",
        "forward_client_headers_to_llm_api",
        "forward_llm_provider_auth_headers",
        "store_prompts_in_spend_logs",
        "allow_public_health_readiness_details",
        "database_connection_pool_limit",
    }
    assert set(config["litellm_settings"]) == {
        "turn_off_message_logging",
        "request_timeout",
        "cache",
        "cache_params",
        "enable_redis_auth_cache",
    }


def test_redis_is_configured_so_shared_state_actually_works() -> None:
    """Setting REDIS_URL alone is not enough: the config must point at Redis."""

    settings = load_yaml("config.yaml")["litellm_settings"]
    assert settings["cache"] is True
    assert settings["cache_params"]["type"] == "redis"
    assert settings["enable_redis_auth_cache"] is True


def test_connection_pool_fits_the_database_ceiling() -> None:
    pool = load_yaml("config.yaml")["general_settings"]["database_connection_pool_limit"]
    web = service()
    workers = int(env_vars()["LITELLM_NUM_WORKERS"]["value"])
    assert pool * workers * web["numInstances"] <= POSTGRES_CONNECTION_CAP
    assert load_yaml("config.yaml")["litellm_settings"]["request_timeout"] <= 900


def test_general_settings_never_turn_client_headers_into_provider_credentials() -> None:
    general = load_yaml("config.yaml")["general_settings"]
    assert general["forward_client_headers_to_llm_api"] is False
    assert general["forward_llm_provider_auth_headers"] is False


def test_config_keeps_prompts_and_verbose_logging_off() -> None:
    config = load_yaml("config.yaml")
    assert config["general_settings"]["store_prompts_in_spend_logs"] is False
    assert config["litellm_settings"]["turn_off_message_logging"] is True
    rendered = yaml.safe_dump(config)
    assert "set_verbose" not in rendered
    assert "detailed_debug" not in rendered


def test_config_has_no_fallbacks_of_any_kind() -> None:
    config = load_yaml("config.yaml")
    assert "router_settings" not in config
    rendered = yaml.safe_dump(config)
    for forbidden in (
        "fallbacks",
        "budget_fallbacks",
        "context_window_fallbacks",
        "content_policy_fallbacks",
        "model_max_budget",
    ):
        assert forbidden not in rendered


def blueprint_services() -> dict[str, dict]:
    return {entry["name"]: entry for entry in load_yaml("render.yaml")["services"]}


def service() -> dict:
    return blueprint_services()["litellm-azure-proxy"]


def env_vars() -> dict[str, dict]:
    return {entry["key"]: entry for entry in service()["envVars"]}


def test_web_service_shape() -> None:
    web = service()
    assert web["type"] == "web"
    assert web["runtime"] == "docker"
    assert web["region"] == "virginia"
    assert web["dockerContext"] == "."
    assert web["dockerfilePath"] == "./Dockerfile"
    assert web["healthCheckPath"] == "/health/readiness"
    assert web["plan"] != "free"


def test_web_plan_meets_the_documented_per_worker_floor() -> None:
    """LiteLLM documents 1 vCPU and 4 GB per worker; 4 GB is a floor, not a target."""

    plan = service()["plan"]
    workers = int(env_vars()["LITELLM_NUM_WORKERS"]["value"])
    cpu, _, memory = plan.partition("c-")
    assert float(cpu) >= workers
    assert int(memory.rstrip("g")) >= 4 * workers


def test_in_flight_requests_get_time_to_drain_on_redeploy() -> None:
    assert service()["maxShutdownDelaySeconds"] >= 60


def test_more_than_one_worker_is_backed_by_shared_redis() -> None:
    web = service()
    if web["numInstances"] > 1 or int(env_vars()["LITELLM_NUM_WORKERS"]["value"]) > 1:
        assert "REDIS_URL" in env_vars()
        assert "litellm-cache" in blueprint_services()


def test_something_always_owns_the_schema_migration() -> None:
    """Either startup migrates (schema update enabled), or a separate job does.

    Never both disabled, and never two instances racing startup migrations.
    """

    web = service()
    variables = env_vars()
    startup_migrates = "DISABLE_SCHEMA_UPDATE" not in variables
    job_migrates = "--skip_server_startup" in web.get("preDeployCommand", "")

    assert startup_migrates or job_migrates, "nothing would ever create the schema"
    if startup_migrates and not job_migrates:
        assert web["numInstances"] == 1, (
            "multiple instances would race on startup migrations; move migrations "
            "to a one-off job first"
        )


def test_non_secret_env_vars_have_the_required_values() -> None:
    variables = env_vars()
    assert variables["PORT"]["value"] == "4000"
    assert variables["STORE_MODEL_IN_DB"]["value"] == "True"
    assert variables["LITELLM_LOG"]["value"] == "INFO"
    assert variables["LITELLM_MODE"]["value"] == "PRODUCTION"


def test_env_vars_are_exactly_the_reviewed_set() -> None:
    assert set(env_vars()) == {
        "PORT",
        "STORE_MODEL_IN_DB",
        "LITELLM_LOG",
        "LITELLM_MODE",
        "LITELLM_NUM_WORKERS",
        "AZURE_SCOPE",
        "DATABASE_URL",
        "REDIS_URL",
        *SECRET_ENV_KEYS,
    }


def test_datastores_are_wired_from_the_managed_instances() -> None:
    variables = env_vars()
    assert variables["DATABASE_URL"]["fromDatabase"] == {
        "name": "litellm-postgres",
        "property": "connectionString",
    }
    assert variables["REDIS_URL"]["fromService"] == {
        "name": "litellm-cache",
        "type": "keyvalue",
        "property": "connectionString",
    }


def test_every_secret_is_sync_false_with_no_default_value() -> None:
    variables = env_vars()
    for key in SECRET_ENV_KEYS:
        assert variables[key]["sync"] is False, key
        assert "value" not in variables[key], key


def test_key_value_instance_is_private_only() -> None:
    cache = blueprint_services()["litellm-cache"]
    assert cache["type"] == "keyvalue"
    assert cache["region"] == "virginia"
    assert cache["ipAllowList"] == []
    assert cache["plan"] != "free"


def test_database_shape() -> None:
    databases = load_yaml("render.yaml")["databases"]
    assert len(databases) == 1
    database = databases[0]
    assert database["name"] == "litellm-postgres"
    assert database["databaseName"] == "litellm"
    assert database["user"] == "litellm"
    assert database["postgresMajorVersion"] == "16"
    assert database["region"] == "virginia"
    assert database["plan"] == "4c-16g"


def test_every_component_shares_one_region() -> None:
    regions = {entry["region"] for entry in blueprint_services().values()}
    regions |= {entry["region"] for entry in load_yaml("render.yaml")["databases"]}
    assert regions == {"virginia"}


def test_gitignore_covers_secrets_and_generated_keys() -> None:
    entries = {line.strip() for line in read(".gitignore").splitlines()}
    for required in (
        ".env",
        ".env.*",
        "!.env.example",
        "generated-keys.json",
        "__pycache__/",
        "*.pyc",
        ".DS_Store",
        ".vscode/",
        ".idea/",
        ".pytest_cache/",
        "coverage.xml",
        "htmlcov/",
    ):
        assert required in entries, required


def test_env_example_is_tracked_and_holds_placeholders_only() -> None:
    example = ROOT / ".env.example"
    assert example.is_file()
    text = example.read_text(encoding="utf-8")
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "LITELLM_MASTER_KEY",
        "LITELLM_SALT_KEY",
        "AZURE_OPENAI_API_BASE",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "PORT=4000",
        "STORE_MODEL_IN_DB=True",
    ):
        assert name in text, name
    for line in text.splitlines():
        if "sk-" in line and not line.strip().startswith("#"):
            assert "REPLACE" in line, line
    assert "AZURE_OPENAI_API_VERSION" not in text
    assert "openai.azure.com" in text
    assert "/openai/v1" in text


def test_generated_keys_file_is_not_committed() -> None:
    assert not (ROOT / "generated-keys.json").exists()


def test_aliases_and_access_group_are_consistent_across_the_repository() -> None:
    assert [entry["model_name"] for entry in models()] == list(ALL_ALIASES)
    readme = read("README.md")
    for alias in ALL_ALIASES:
        assert alias in readme, alias
    assert CUSTOMER_ACCESS_GROUP in readme
    assert ALL_PROXY_MODELS in readme  # documented as what we deliberately avoid
    assert CUSTOMER_ACCESS_GROUP in read("SECURITY.md")
