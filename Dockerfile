# Thin Render wrapper around the official LiteLLM image. Nothing is vendored, and
# the tag is pinned: never latest, never main-stable.
ARG LITELLM_VERSION=v1.99.0
FROM ghcr.io/berriai/litellm-database:${LITELLM_VERSION}

WORKDIR /app

# No secrets in build args or layers; Render injects every credential at runtime.
COPY config.yaml /app/config.yaml
COPY scripts/render_start.sh /app/scripts/render_start.sh

EXPOSE 4000

# No USER line: this image defines no non-root user, and forcing an arbitrary UID
# breaks the startup migration. The non-root variant is a separate image,
# litellm-non_root, which does not bundle Prisma.
# No CMD: render_start.sh supplies every flag, which a CMD would shadow.
# The entrypoint is replaced so the environment is validated before the CLI runs.
ENTRYPOINT ["/bin/sh", "/app/scripts/render_start.sh"]
