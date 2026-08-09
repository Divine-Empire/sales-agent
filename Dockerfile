# Official uv image — uv is the only package manager this project uses.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependencies first, in their own layer: application edits then rebuild in
# seconds instead of re-resolving the whole tree.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app/ ./app/
COPY data/ ./data/

# Non-root. The container has API keys in its environment; there is no reason
# for the process to run as root.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 10000

# Shell form so $PORT expands — Render injects it at runtime and it is not
# known at build time. Defaults to 10000 for local `docker run`.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}
