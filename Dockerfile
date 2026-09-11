# Build PostDelegate's Tailwind assets without carrying Node into production.
FROM node:20-slim AS frontend
WORKDIR /src/theme/static_src
COPY theme/static_src/package*.json ./
RUN npm ci
COPY theme/static_src/ ./
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

# ffmpeg/ffprobe are required for video validation and metadata extraction.
# Keep build tools, Node, linters, type-checkers and test frameworks out of the
# runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /install /usr/local
COPY . .
COPY --from=frontend /src/theme/static/css/dist/styles.css /app/theme/static/css/dist/styles.css

RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    SECRET_KEY=build-placeholder \
    DATABASE_URL=sqlite:///tmp/build.db \
    python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 2"]
