# The LLM sandbox: one short-lived container per execution (ADR-008).
# Holds no Axiom or GitHub credentials. Network: internal Docker network + egress proxy only.
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /opt/axiom
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra sandbox --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra sandbox

# HOME is a tmpfs at run time (no user logins or settings can exist in it).
ENV PATH="/opt/axiom/.venv/bin:$PATH" \
    HOME=/home/agent \
    UV_CACHE_DIR=/home/agent/.cache/uv \
    PYTHONUNBUFFERED=1
WORKDIR /workspace/repo
ENTRYPOINT ["python", "-m", "axiom.sandbox.runner"]
