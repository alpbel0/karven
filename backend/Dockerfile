FROM python:3.12-slim

# Install uv by copying the static binary from the official image.
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install dependencies from the lockfile (no dev deps) first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
# Migrations are run at runtime by `python -m app.migrator`.
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
