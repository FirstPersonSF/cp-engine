FROM python:3.14-slim

# git + ssh for the read-only tenant clone (tree tools); GIT_SSH_KEY env
# carries a read-only deploy key at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# server.py imports observability at module scope — both must ship, or the
# container dies on ImportError at startup. Listed explicitly rather than
# `COPY . .` so test files and __pycache__ stay out of the image.
COPY server.py observability.py ./
# The four wrap-up verbs import `cp_engine.<module>` at call time and this
# image installs no cp-engine (its dependency tree — anthropic, pypdf,
# slack-sdk, four private git deps — dwarfs this server's six packages). The
# vendored closure is 76K of pure-Python lint modules; `tests/test_vendor_drift.py`
# fails if any copy stops matching its source. Without this COPY, three of the
# four verbs raise ModuleNotFoundError at call time while still REGISTERING,
# so /health reports them present and every invocation fails (#283).
COPY vendor ./vendor

# Railway injects PORT; bind all interfaces behind its proxy.
ENV HOST=0.0.0.0

CMD ["python", "server.py"]
