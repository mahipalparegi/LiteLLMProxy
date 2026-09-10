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

REQUIRED='DATABASE_URL LITELLM_MASTER_KEY LITELLM_SALT_KEY AZURE_TENANT_ID AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_OPENAI_API_BASE'

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

# The portal's "Azure OpenAI endpoint" field shows the v1 surface, ending
# /openai/v1. The azure/ route builds /openai/deployments/... itself, so it needs
# the resource root. Normalise rather than reject: the suffix and any trailing
# slash are what the portal hands the operator, not operator error.
while :; do
    case "${AZURE_OPENAI_API_BASE}" in
        */) AZURE_OPENAI_API_BASE="${AZURE_OPENAI_API_BASE%/}" ;;
        */openai/v1) AZURE_OPENAI_API_BASE="${AZURE_OPENAI_API_BASE%/openai/v1}" ;;
        */openai) AZURE_OPENAI_API_BASE="${AZURE_OPENAI_API_BASE%/openai}" ;;
        *) break ;;
    esac
done
export AZURE_OPENAI_API_BASE

case "${AZURE_OPENAI_API_BASE}" in
    *' '*|*'?'*|*'/deployments'*)
        fail 'AZURE_OPENAI_API_BASE must be the resource endpoint only, with no /deployments/... path and no query string' ;;
    */api/projects/*)
        fail 'AZURE_OPENAI_API_BASE is the Foundry PROJECT endpoint. Azure OpenAI models need the resource endpoint instead: https://<resource>.openai.azure.com' ;;
    https://*.openai.azure.com|https://*.services.ai.azure.com) ;;
    *) fail 'AZURE_OPENAI_API_BASE must be https://<resource>.openai.azure.com - copy the portal Azure OpenAI endpoint; a /openai/v1 suffix is stripped for you' ;;
esac
log 'ok: AZURE_OPENAI_API_BASE normalised to the resource endpoint'

# api_version is deliberately not required: LiteLLM v1.99.0 defaults to
# 2025-02-01-preview. Validate only if the operator overrides it.
if [ -n "${AZURE_API_VERSION-}" ]; then
    case "${AZURE_API_VERSION}" in
        ????-??-??|????-??-??-preview|preview|latest) ;;
        *) fail 'AZURE_API_VERSION must look like 2025-02-01 or 2025-02-01-preview' ;;
    esac
    log 'ok: AZURE_API_VERSION override has an api-version shape'
fi

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
