FROM python:3.11-slim

# Conservative anti-ban presets for containerised deployments (the app
# defaults are hotter: 0.3s interval / 5 concurrency). Not listed in
# docker-compose on purpose — users shouldn't tune these. Overridable via
# compose env_file / docker -e (env_file wins over image ENV).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME_CONFIG=/config/home.toml \
    HTML_INTERVAL_SECONDS=1.5 \
    MAX_CONCURRENCY=2 \
    IMAGE_MAX_CONCURRENCY=5 \
    THUMB_MAX_CONCURRENCY=25

WORKDIR /srv/pandaopds

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir .

RUN mkdir -p /config

EXPOSE 8000

# Runtime config is injected via environment (docker-compose / secrets).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
