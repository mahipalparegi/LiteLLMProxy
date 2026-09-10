"""Shared standard-library helpers for the operator scripts.

Standard library only. Anything that could carry a credential goes through
redact() before it is printed or placed in an exception message.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

OPENAI_ALIASES: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
# Configured but kept out of the customer group; see config.yaml for why.
PREVIEW_ALIASES: tuple[str, ...] = ("gpt-6-astra",)

# Customer-visible aliases.
PROXY_ALIASES: tuple[str, ...] = OPENAI_ALIASES
ALL_ALIASES: tuple[str, ...] = PROXY_ALIASES + PREVIEW_ALIASES

# azure/ is Azure OpenAI. azure_ai/ stays accepted so a Foundry model outside the
# OpenAI family can be added later without loosening the check.
AZURE_PREFIXES: tuple[str, ...] = ("azure_ai/", "azure/")

CUSTOMER_ACCESS_GROUP = "customer-models"
PREVIEW_ACCESS_GROUP = "admin-preview"

# LiteLLM sentinel granting every model on the proxy. Deliberately unused on
# customer keys.
ALL_PROXY_MODELS = "all-proxy-models"

SECRET_ENV_NAMES: tuple[str, ...] = (
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "LITELLM_API_KEY",
    "DATABASE_URL",
    "AZURE_CLIENT_SECRET",
)

REDACTED = "***REDACTED***"

_SECRETS: set[str] = set()
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")


def register_secret(value: str | None) -> None:
    """Remember a value so redact() scrubs it from any later output."""

    if value and len(value) >= 6:
        _SECRETS.add(value)


def register_environment_secrets(names: Iterable[str] = SECRET_ENV_NAMES) -> None:
    for name in names:
        register_secret(os.getenv(name))


def redact(text: Any) -> str:
    rendered = text if isinstance(text, str) else str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        rendered = rendered.replace(secret, REDACTED)
    rendered = _KEY_PATTERN.sub(REDACTED, rendered)
    return _URL_CREDENTIALS.sub(REDACTED, rendered)


def mask_key(value: str) -> str:
    """Return a hint for a key, never the key itself."""

    if not value:
        return "<empty>"
    return f"<key ending {value[-4:]}>" if len(value) > 4 else "<key>"


class HttpError(RuntimeError):
    """A non-2xx HTTP response. The message is always redacted."""

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = redact(url)
        self.body = redact(body)[:500]
        super().__init__(f"HTTP {status} from {self.url}: {self.body}")


class ContractError(RuntimeError):
    """A deployment or verification contract was not satisfied."""


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise ContractError(f"environment variable {name} must be set to a non-empty value")
    register_secret(value)
    return value.strip()


def base_url(explicit: str | None = None) -> str:
    value = explicit or os.getenv("LITELLM_BASE_URL")
    if not value or not value.strip():
        raise ContractError("set LITELLM_BASE_URL (or pass --base-url) to the proxy URL")
    return value.strip().rstrip("/")


def positive_number(value: Any, *, name: str, integer: bool = False) -> float | int:
    """Return value as a finite number greater than zero, or raise ValueError.

    Rejects None, empty strings, booleans, non-numeric text, NaN, +/-inf, zero
    and negatives. With integer=True, also rejects fractional values.
    """

    if value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not a boolean")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{name} is required")
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{name} must be a number, got {text!r}") from None
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {type(value).__name__}")

    numeric = float(value)
    if math.isnan(numeric):
        raise ValueError(f"{name} must be a finite number, got NaN")
    if math.isinf(numeric):
        raise ValueError(f"{name} must be a finite number, got infinity")
    if numeric <= 0:
        raise ValueError(f"{name} must be greater than zero, got {numeric:g}")
    if integer:
        if not numeric.is_integer():
            raise ValueError(f"{name} must be a whole number, got {numeric:g}")
        return int(numeric)
    return numeric


def http_request(
    method: str,
    url: str,
    *,
    payload: Any = None,
    bearer: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], Any]:
    """Make a JSON request. Returns (status, response headers, decoded body).

    Raises HttpError on a non-2xx status. Authorization values are never echoed.
    """

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if bearer:
        register_secret(bearer)
        request_headers["Authorization"] = f"Bearer {bearer}"
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, dict(response.headers.items()), decode_json(raw)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        raise HttpError(error.code, url, raw) from None
    except urllib.error.URLError as error:
        raise ContractError(f"request to {redact(url)} failed: {redact(error.reason)}") from None


def try_http_request(*args: Any, **kwargs: Any) -> tuple[int, dict[str, str], Any]:
    """Like http_request, but returns the status instead of raising on non-2xx."""

    try:
        return http_request(*args, **kwargs)
    except HttpError as error:
        return error.status, {}, error.body


def decode_json(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ContractError(f"expected a JSON response, received: {redact(raw)[:200]}") from None


def model_entries(payload: Any) -> list[dict[str, Any]]:
    """Normalise /v1/models and /model/info payloads into a list of dicts."""

    data = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def public_model_name(entry: dict[str, Any]) -> str | None:
    for field in ("id", "model_name"):
        value = entry.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def underlying_model(entry: dict[str, Any]) -> str | None:
    """Return litellm_params.model for a /model/info entry, if present."""

    params = entry.get("litellm_params")
    if isinstance(params, dict):
        value = params.get("model")
        if isinstance(value, str) and value:
            return value
    return None


def access_groups(entry: dict[str, Any]) -> list[str]:
    """Return model_info.access_groups for a /model/info entry."""

    info = entry.get("model_info")
    if isinstance(info, dict):
        groups = info.get("access_groups")
        if isinstance(groups, list):
            return [group for group in groups if isinstance(group, str)]
    return []


def is_azure_backed(model: str) -> bool:
    return model.startswith(AZURE_PREFIXES)


def token_limit_param(alias: str) -> str:
    """Return the output-token parameter this model accepts.

    The GPT-5 reasoning line and GPT-6 take max_completion_tokens and reject
    max_tokens. On LiteLLM v1.99.0 the classifier that rewrites this matches
    gpt-5* only, so gpt-6* callers must send the right field themselves.
    """

    return "max_completion_tokens" if alias.startswith("gpt-6") else "max_tokens"


def missing_aliases(names: Iterable[str]) -> list[str]:
    available = set(names)
    return [alias for alias in PROXY_ALIASES if alias not in available]


def write_secret_json(path: str | Path, value: Any, *, force: bool = False) -> Path:
    """Write JSON with 0600 permissions, refusing to clobber unless forced."""

    destination = Path(path)
    if destination.exists() and not force:
        raise ContractError(
            f"{destination} already exists. It may hold live customer keys. "
            "Move it into your secret store first, or pass --force to overwrite."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        _restrict_permissions(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _restrict_permissions(destination)
    return destination


def _restrict_permissions(path: Path) -> None:
    # Windows has no POSIX mode bits, so failure here is tolerated.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass


def file_mode(path: str | Path) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)
