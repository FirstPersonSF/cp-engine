# syntax=docker/dockerfile:1
# cp-engine cron trigger — a Railway cron service's image (docs/railway-cron.md).
#
# Build context: the cp-engine repo root (Railway's default). Point the
# service at this file with the service variable
#   RAILWAY_DOCKERFILE_PATH=webhook/cron/Dockerfile
# Standard library only: no pip install, no private deps, no build token.
FROM python:3.11-slim
WORKDIR /app
COPY webhook/cron/trigger.py ./trigger.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "trigger.py"]
