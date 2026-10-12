FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install ".[gcp]" && rm -rf app

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --create-home appuser
USER appuser

# Cloud Run injects $PORT. Migrations run as a separate Cloud Run Job, not on boot,
# so scaling out never races schema changes.
CMD exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} \
    --proxy-headers --forwarded-allow-ips="*"
