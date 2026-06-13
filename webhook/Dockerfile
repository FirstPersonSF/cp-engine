# syntax=docker/dockerfile:1
# cp-engine-webhook — Phase C.2
#
# Build context is the cp-engine repo root (set in railway.toml's
# `dockerfilePath = "webhook/Dockerfile"` + `buildDir = "."`).
# We need the cp_engine package source available so the webhook can
# pip install ../ to pick up the same code we ship as the CLI.
#
# BUILD SECRET — gh_token (REQUIRED):
#   cp-engine's deps include private git+https refs to
#   github.com/Canonic-OS/canonic-component-library, so the build stage
#   needs GitHub read auth. We mount a token as a BuildKit secret
#   (id=gh_token) and use it via git's insteadOf rewrite ONLY inside the
#   install RUN — it is never baked into an ARG/ENV or image layer.
#
#   Token scope: a READ-ONLY token is sufficient. Either a fine-grained
#   PAT scoped to Canonic-OS/canonic-component-library with
#   "Contents: read", or a machine-user classic PAT with `repo:read`.
#
#   Railway:  Service → Settings → Build → add a build secret named
#             `gh_token` (Railway passes registered build secrets through
#             to BuildKit's --mount=type=secret). The operator must wire
#             this in the Railway dashboard; the build will fail to clone
#             the private deps without it (unless they are already cached).
#
#   Local:    DOCKER_BUILDKIT=1 docker build \
#               --secret id=gh_token,env=GH_TOKEN \
#               -f webhook/Dockerfile -t cp-webhook .
#             (with GH_TOKEN exported to a read-only token)

FROM python:3.11-slim

# git is needed at runtime for the clone-on-each-request strategy.
# openssh-client provides ssh for the deploy-key auth.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install cp-engine first (its deps cache well between builds).
COPY pyproject.toml README.md ./cp-engine/
COPY src ./cp-engine/src
# cp-engine's deps include private git+https refs to
# canonic-component-library, so we need GitHub read auth at build time.
# The token is provided via a BuildKit secret (id=gh_token) and read from
# /run/secrets/gh_token at use-time — it is NEVER written to an image
# layer, ARG, or ENV. git's insteadOf rewrite injects it into the clone
# URL only for the duration of this RUN; we unset it again afterwards.
RUN --mount=type=secret,id=gh_token \
    sh -c 'if [ -f /run/secrets/gh_token ]; then \
             git config --global url."https://$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/"; \
           fi && \
           pip install --no-cache-dir ./cp-engine && \
           git config --global --unset url."https://$(cat /run/secrets/gh_token)@github.com/".insteadOf || true'

# Install the webhook itself.
COPY webhook ./webhook
RUN pip install --no-cache-dir ./webhook

# Railway injects $PORT at runtime — must use shell form (or sh -c) so
# the variable expands. Exec form treats the literal string "$PORT" as
# a value, which is why the first deploy 502'd.
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir webhook"]
