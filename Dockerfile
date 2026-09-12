# Build PostDelegate's Tailwind assets without carrying Node into production.
FROM node:22-slim AS frontend
WORKDIR /src/theme/static_src
COPY theme/static_src/package*.json ./
RUN npm ci
COPY theme/static_src/ ./
# Tailwind source paths resolve against /src/theme/static_src/src/styles.css.
COPY templates/ /src/templates/
COPY apps/ /src/apps/
RUN npm run build

# Install Python packages into a relocatable prefix. No compiler is needed for
# the production dependency set because supported platforms use binary wheels.
FROM python:3.12-slim AS python-deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements-prod.txt .
RUN pip install --prefix=/install -r requirements-prod.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app

# ffmpeg/ffprobe are required for video validation and metadata extraction.
# Keep build tools, Node, linters, type-checkers and test frameworks out of the
# runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /install /usr/local
COPY --chown=app:app . .
COPY --from=frontend --chown=app:app /src/theme/static/css/dist/styles.css /app/theme/static/css/dist/styles.css

RUN chown app:app /app
USER app

RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
    ENCRYPTION_KEY_SALT="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
    DATABASE_URL=sqlite:///tmp/build.db \
    python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 2"]
