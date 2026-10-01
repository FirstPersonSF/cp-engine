# syntax=docker/dockerfile:1
# hosted-cp MCP server.
#
# BUILD CONTEXT IS THE cp-engine REPO ROOT, not this directory (architecture
# plan step 1). The image installs cp-engine as a package from the SAME
# checkout as server.py, the way webhook/Dockerfile does, so the hosted verbs
# run the engine's code rather than a hand-vendored copy of it. The engine
# version is therefore pinned by construction: whatever commit `deploy.sh`
# stages is both the server and the engine it imports.
#
# Deploy with `prototypes/hosted-mcp/deploy.sh` (see railway.toml). It stages
# exactly the paths this file COPYs from `git archive HEAD` and uploads that.
# A bare `railway up` from prototypes/hosted-mcp/ uploads a context with no
# `src/`, so the `COPY src` below fails the build loudly instead of shipping
# a server with no engine.
#
# Local build (from the repo root; `Dockerfile.dockerignore` keeps .venv etc.
# out of the context):
#   GH_TOKEN="$(gh auth token)" docker build --build-arg gh_token="$GH_TOKEN" \
#     -f prototypes/hosted-mcp/Dockerfile -t hosted-cp .
# or `prototypes/hosted-mcp/deploy.sh --build-local`, which builds the exact
# staged context Railway would receive.
#
# BUILD AUTH — `gh_token` (REQUIRED). cp-engine depends on private
# git+https refs to FirstPersonSF/1p-component-library. Railway passes a
# service variable named `gh_token` (lowercase) to the build as this ARG; it
# does not support BuildKit secret mounts. Read-only scope is enough
# (Contents:read on 1p-component-library). The token reaches git only through
# GIT_CONFIG_* environment variables scoped to the one pip RUN — nothing is
# written to ~/.gitconfig, so no image layer carries it.

FROM python:3.14-slim

# git: pip's git+https deps at build time, and the read-only tenant clone at
# runtime (tree tools). openssh-client: GIT_SSH_KEY carries a read-only
# deploy key for that clone.
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# cp-engine first: its dependency tree is the slow part and caches well.
COPY pyproject.toml README.md ./cp-engine/
COPY src ./cp-engine/src
COPY prototypes/hosted-mcp/requirements.txt ./requirements.txt
ARG gh_token
RUN sh -c 'if [ -n "$gh_token" ]; then \
             export GIT_CONFIG_COUNT=1 \
                    GIT_CONFIG_KEY_0="url.https://${gh_token}@github.com/.insteadOf" \
                    GIT_CONFIG_VALUE_0="https://github.com/"; \
           fi && \
           pip install --no-cache-dir ./cp-engine -r requirements.txt' \
    && rm -rf ./cp-engine

# server.py loads observability.py and ported_tools.py by path at module
# scope — all three must ship.
# Listed explicitly so tests and __pycache__ stay out of the image.
# BUILD_COMMIT is written by deploy.sh: the commit this image was staged
# from, which /health reports as `commit` (Railway injects no SHA
# for a CLI upload). The glob keeps a plain local `docker build` working
# without it.
COPY prototypes/hosted-mcp/server.py prototypes/hosted-mcp/observability.py \
     prototypes/hosted-mcp/ported_tools.py prototypes/hosted-mcp/BUILD_COMMI[T] ./

# Railway injects PORT; bind all interfaces behind its proxy.
ENV HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

CMD ["python", "server.py"]
