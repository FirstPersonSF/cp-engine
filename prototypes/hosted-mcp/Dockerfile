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

# Railway injects PORT; bind all interfaces behind its proxy.
ENV HOST=0.0.0.0

CMD ["python", "server.py"]
