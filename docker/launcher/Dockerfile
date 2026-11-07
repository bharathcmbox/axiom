# The sandbox launcher (ADR-016): the only image with the Docker CLI, and the only service with the
# Docker socket. Runs no LLM and no repository code.
FROM python:3.14-slim

COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" HOME=/tmp
CMD ["python", "-m", "axiom.launcher"]
