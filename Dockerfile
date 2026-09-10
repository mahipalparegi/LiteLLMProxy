# Thin Render wrapper around the official LiteLLM image. Nothing is vendored.
# Tag is pinned deliberately: never latest, never main-stable.
ARG LITELLM_VERSION=v1.99.0
FROM ghcr.io/berriai/litellm-database:${LITELLM_VERSION}

WORKDIR /app

# No secrets in build args or layers; Render injects every credential at runtime.
COPY config.yaml /app/config.yaml
COPY scripts/render_start.sh /app/scripts/render_start.sh

EXPOSE 4000

# No USER line: the litellm-database image defines no non-root user, and forcing
# an arbitrary UID breaks the Prisma migration that runs at startup. The non-root
# variant is a separate image, litellm-non_root, which does not bundle Prisma.
#
# No CMD: render_start.sh supplies every flag including --port "${PORT:-4000}",
# which a hardcoded CMD would shadow.
#
# The upstream entrypoint runs the Prisma helper then the CLI. It is replaced so
# the deployment contract is validated first. Migrations move to
# preDeployCommand in render.yaml.
ENTRYPOINT ["/bin/sh", "/app/scripts/render_start.sh"]
