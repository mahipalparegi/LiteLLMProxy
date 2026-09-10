#!/bin/sh
# Startup contract. Validates the environment, then execs the LiteLLM CLI.
# Prints variable names only, never values, so no secret reaches the deploy log.
set -eu

log() {
    printf '%s\n' "render_start: $1" >&2
}

fail() {
    log "FATAL: $1"
    exit 1
}

REQUIRED='DATABASE_URL LITELLM_MASTER_KEY LITELLM_SALT_KEY AZURE_TENANT_ID AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_API_BASE AZURE_OPENAI_API_BASE AZURE_OPENAI_API_VERSION'

for name in ${REQUIRED}; do
    eval "current=\${${name}-}"
    if [ -z "${current}" ]; then
        fail "required environment variable is missing or empty: ${name}"
    fi
    log "ok: ${name} is set"
done
unset current

case "${LITELLM_MASTER_KEY}" in
    sk-*) ;;
    *) fail 'LITELLM_MASTER_KEY must start with sk-' ;;
esac

case "${LITELLM_SALT_KEY}" in
    sk-*) ;;
    *) fail 'LITELLM_SALT_KEY must start with sk-' ;;
esac

if [ "${LITELLM_MASTER_KEY}" = "${LITELLM_SALT_KEY}" ]; then
    fail 'LITELLM_MASTER_KEY and LITELLM_SALT_KEY must be different values'
fi
log 'ok: master key and salt key are distinct and correctly prefixed'

case "${DATABASE_URL}" in
    postgres://*|postgresql://*) ;;
    *) fail 'DATABASE_URL must be a PostgreSQL connection string (postgres:// or postgresql://)' ;;
esac
log 'ok: DATABASE_URL is a PostgreSQL connection string'

# LiteLLM appends /v1/messages itself, so the Claude base must not carry it.
case "${AZURE_API_BASE}" in
    https://*.services.ai.azure.com/anthropic) ;;
    *) fail 'AZURE_API_BASE must be exactly https://<resource>.services.ai.azure.com/anthropic (no /v1/messages suffix, no model name, no trailing slash)' ;;
esac
log 'ok: AZURE_API_BASE has the documented Foundry Anthropic shape'

# Azure OpenAI is a different path on the resource, so a separate variable.
case "${AZURE_OPENAI_API_BASE}" in
    *' '*|*'?'*|*'/openai/deployments'*)
        fail 'AZURE_OPENAI_API_BASE must be the resource endpoint only, with no /openai/deployments/... path and no query string' ;;
    https://*.openai.azure.com/|https://*.openai.azure.com|https://*.services.ai.azure.com/|https://*.services.ai.azure.com) ;;
    *) fail 'AZURE_OPENAI_API_BASE must be https://<resource>.openai.azure.com/ or https://<resource>.services.ai.azure.com/' ;;
esac
log 'ok: AZURE_OPENAI_API_BASE has an Azure OpenAI resource shape'

case "${AZURE_OPENAI_API_VERSION}" in
    ????-??-??|????-??-??-preview) ;;
    *) fail 'AZURE_OPENAI_API_VERSION must look like 2024-10-21 or 2024-10-21-preview; copy it from the deployment target URI' ;;
esac
log 'ok: AZURE_OPENAI_API_VERSION has an api-version shape'

# Optional for the operator, but config.yaml references it.
if [ -z "${AZURE_SCOPE-}" ]; then
    AZURE_SCOPE='https://cognitiveservices.azure.com/.default'
    export AZURE_SCOPE
    log 'ok: AZURE_SCOPE not supplied, using the documented default scope'
else
    log 'ok: AZURE_SCOPE is set'
fi

WORKERS="${LITELLM_NUM_WORKERS:-4}"
case "${WORKERS}" in
    ''|*[!0-9]*) fail 'LITELLM_NUM_WORKERS must be a positive integer' ;;
esac
[ "${WORKERS}" -ge 1 ] || fail 'LITELLM_NUM_WORKERS must be at least 1'

# Without shared Redis each worker holds its own rate-limit counters and budget
# reservations, multiplying every key limit by the worker count.
if [ "${WORKERS}" -gt 1 ] && [ -z "${REDIS_URL-}${REDIS_HOST-}" ]; then
    fail "LITELLM_NUM_WORKERS=${WORKERS} requires REDIS_URL or REDIS_HOST; without shared Redis, per-key limits and budgets are enforced per worker"
fi
log "ok: ${WORKERS} worker(s)"

log "starting litellm on 0.0.0.0:${PORT:-4000}"

exec litellm \
    --config /app/config.yaml \
    --host 0.0.0.0 \
    --port "${PORT:-4000}" \
    --num_workers "${WORKERS}" \
    "$@"
