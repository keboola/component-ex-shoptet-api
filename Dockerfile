FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /code/
COPY pyproject.toml uv.lock ./

ENV UV_PROJECT_ENVIRONMENT="/usr/local/"
RUN uv sync --no-dev --frozen

COPY src/ src/
COPY scripts/ scripts/

FROM base AS test
RUN uv sync --all-groups --frozen
COPY tests/ tests/
# Formatting is checked in CI as well as pre-commit: pre-commit is bypassable with
# --no-verify, and `ruff format` is deterministic, so enforcing it here costs nothing.
# `ty check` is deliberately NOT run here — ty is pre-1.0 and unpinned, so an upstream
# release could redden CI on untouched code. It stays a local pre-commit gate.
RUN uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
CMD ["uv", "run", "pytest", "tests/", "-v"]

FROM base AS production
CMD ["python", "-u", "/code/src/component.py"]
