# syntax=docker/dockerfile:1
# cp-engine-webhook — Phase C.2
#
# Build context is the cp-engine repo root (set in railway.toml's
# `dockerfilePath = "webhook/Dockerfile"` + `buildDir = "."`).
# We need the cp_engine package source available so the webhook can
# pip install ../ to pick up the same code we ship as the CLI.
#
# BUILD AUTH — gh_token (REQUIRED):
#   cp-engine's deps include private git+https refs to
#   github.com/Canonic-OS/canonic-component-library, so the build stage
#   needs GitHub read auth. The token is accepted via EITHER of two
#   mechanisms, the secret being preferred:
#     1. A BuildKit secret (id=gh_token) — preferred; never appears in
#        image history.
#     2. A build ARG `gh_token` — fallback for platforms (e.g. Railway)
#        that expose service variables to the build as ARGs but do NOT
#        support BuildKit secret mounts. NOTE: a referenced ARG value can
#        appear in `docker history` of the build; acceptable for a
#        read-only token on a private builder, but prefer the secret where
#        supported.
#   In both paths the token is consumed via git's insteadOf rewrite ONLY
#   inside the install RUN — it is never written to an ENV or persisted
#   config (the rewrite is unset at the end of the RUN).
#
#   Token scope: a READ-ONLY token is sufficient. Either a fine-grained
#   PAT scoped to Canonic-OS/canonic-component-library with
#   "Contents: read", or a machine-user classic PAT with `repo:read`.
#
#   Railway:  Service → Variables → add a service variable named
#             `gh_token` (lowercase). Railway passes service variables to
#             the Docker build as build ARGs (because the Dockerfile
#             declares `ARG gh_token`). Railway does NOT support BuildKit
#             secret mounts, so the ARG path is the one that applies here.
#             Without the token the build fails to clone the private deps
#             (unless they are already cached).
#
#   Local:    DOCKER_BUILDKIT=1 docker build \
#               --secret id=gh_token,env=GH_TOKEN \
#               -f webhook/Dockerfile -t cp-webhook .
#             (with GH_TOKEN exported to a read-only token; preferred)
#             — or, mirroring the Railway path —
#             docker build --build-arg gh_token=$GH_TOKEN \
#               -f webhook/Dockerfile -t cp-webhook .

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
# Token for cloning private canonic-component-library deps. Supplied as a build
# ARG `gh_token` (Railway exposes service variables to the Docker build as ARGs).
# Railway's builder does NOT support BuildKit `--mount=type=secret` (it rejects
# any mount type other than `type=cache`), so the token comes via ARG only. It's
# consumed inside this single RUN for a git insteadOf rewrite and is NOT written
# to an ENV (so it's not in the running image). Tradeoff: a referenced ARG value
# CAN appear in `docker history` of the build image — acceptable for a read-only
# token (Contents:read on canonic-component-library) on a private builder.
# Operator: set a Railway service variable named `gh_token` (lowercase).
ARG gh_token
RUN sh -c 'if [ -n "$gh_token" ]; then \
             git config --global url."https://${gh_token}@github.com/".insteadOf "https://github.com/"; \
           fi && \
           pip install --no-cache-dir ./cp-engine && \
           git config --global --unset-all url."https://${gh_token}@github.com/".insteadOf 2>/dev/null || true'

# Install the webhook itself.
COPY webhook ./webhook
RUN pip install --no-cache-dir ./webhook

# Railway injects $PORT at runtime — must use shell form (or sh -c) so
# the variable expands. Exec form treats the literal string "$PORT" as
# a value, which is why the first deploy 502'd.
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir webhook"]
