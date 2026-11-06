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

# Other Pythons a project may ask for (projects.python_version), outside HOME and read-only at run
# time. Keep in step with SANDBOX_PYTHONS in registry/projects.py.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN UV_PYTHON_DOWNLOADS=automatic uv python install 3.12 3.13 && chmod -R a+rX /opt/uv-python

# HOME is a tmpfs at run time (no user logins or settings can exist in it).
ENV PATH="/opt/axiom/.venv/bin:$PATH" \
    HOME=/home/agent \
    UV_CACHE_DIR=/home/agent/.cache/uv \
    PYTHONUNBUFFERED=1
WORKDIR /workspace/repo
ENTRYPOINT ["python", "-m", "axiom.sandbox.runner"]
