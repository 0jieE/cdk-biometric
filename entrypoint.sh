#!/bin/bash
# Shared container entrypoint for web / celery_worker / celery_beat.
# Behaviour is driven by env flags so each service does only what it should:
#   RUN_MIGRATIONS       run `migrate` (web only; default true)
#   WAIT_FOR_MIGRATIONS  block until another service has migrated (worker/beat)
#   SEED_ON_START        run idempotent `seed_data` (web only)
#   COLLECT_STATIC       run `collectstatic` (web only)
set -e

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-3306}"

echo "[entrypoint] Waiting for MySQL at ${DB_HOST}:${DB_PORT} ..."
until python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('${DB_HOST}', ${DB_PORT})); s.close()" 2>/dev/null; do
  echo "[entrypoint] MySQL not ready — retrying in 2s"
  sleep 2
done
echo "[entrypoint] MySQL is accepting connections."

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "[entrypoint] Applying migrations ..."
  python manage.py migrate --noinput
fi

if [ "${WAIT_FOR_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] Waiting for migrations to be applied by the web service ..."
  until python -c "import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup(); from django.db import connection; connection.cursor().execute('SELECT 1 FROM django_migrations LIMIT 1')" 2>/dev/null; do
    echo "[entrypoint] Migrations not present yet — retrying in 3s"
    sleep 3
  done
  echo "[entrypoint] Migrations present."
fi

if [ "${SEED_ON_START:-false}" = "true" ]; then
  echo "[entrypoint] Seeding data (idempotent) ..."
  python manage.py seed_data
fi

if [ "${COLLECT_STATIC:-false}" = "true" ]; then
  echo "[entrypoint] Collecting static files ..."
  python manage.py collectstatic --noinput
fi

echo "[entrypoint] Starting: $*"
exec "$@"
