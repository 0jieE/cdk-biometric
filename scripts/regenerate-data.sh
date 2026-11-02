#!/bin/sh
# Bring the Docker stack's data in line with the current schema and attendance
# rules. Same steps as regenerate-data.ps1 (see there for details).
#
#     ./scripts/regenerate-data.sh          # backup, preview, ask, apply
#     ./scripts/regenerate-data.sh -y       # don't ask
#
# Restore a backup with:
#   docker compose cp backups/<file>.sql db:/tmp/restore.sql
#   docker compose exec -T db sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysql -uroot $MYSQL_DATABASE < /tmp/restore.sql'
set -eu
cd "$(dirname "$0")/.."     # the backend folder (docker-compose.yml)

step() { printf '\n== %s\n' "$1"; }

step 'Backing up the database'
stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p backups
docker compose exec -T db sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysqldump -uroot --single-transaction --routines $MYSQL_DATABASE --result-file=/tmp/backup.sql'
docker compose cp db:/tmp/backup.sql "backups/attendance-$stamp.sql"
echo "Saved backups/attendance-$stamp.sql"

step 'Applying migrations'
docker compose exec -T web python manage.py migrate --noinput

step 'Preview (nothing is changed yet)'
docker compose exec -T web python manage.py regenerate_attendance --dry-run

if [ "${1:-}" != "-y" ]; then
    if [ ! -t 0 ]; then     # no terminal to answer from: never assume yes
        echo 'No terminal to confirm from - re-run with -y to apply. Nothing was changed (migrations aside).'
        exit 1
    fi
    printf '\nApply these changes? [y/N] '
    read -r answer || answer=
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo 'Stopped. Nothing was changed (migrations aside).'; exit 0 ;;
    esac
fi

step 'Regenerating attendance'
docker compose exec -T web python manage.py regenerate_attendance

step 'Restarting app containers'
docker compose restart web celery_worker celery_beat attendance_listener

printf '\nDone. Backup: backups/attendance-%s.sql\n' "$stamp"
