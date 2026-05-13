# cp-engine-webhook — Phase C.2
#
# Build context is the cp-engine repo root (set in railway.toml's
# `dockerfilePath = "webhook/Dockerfile"` + `buildDir = "."`).
# We need the cp_engine package source available so the webhook can
# pip install ../ to pick up the same code we ship as the CLI.

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
RUN pip install --no-cache-dir ./cp-engine

# Install the webhook itself.
COPY webhook ./webhook
RUN pip install --no-cache-dir ./webhook

# Railway injects PORT; uvicorn reads it.
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "webhook"]
