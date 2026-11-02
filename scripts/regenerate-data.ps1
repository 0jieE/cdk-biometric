<#
Bring the Docker stack's data in line with the current schema and attendance rules.

    .\scripts\regenerate-data.ps1          # backup, preview, ask, apply
    .\scripts\regenerate-data.ps1 -Yes     # don't ask

Steps:
  1. Back up the database to backups\attendance-<timestamp>.sql
  2. Apply pending migrations
  3. Preview what regenerate_attendance would change (dry run)
  4. Apply it: re-classify stored punches, drop duplicates, re-derive
     absences / lates / undertime / overtime (manual entries are never touched)
  5. Restart the app containers so they run the current code

Restore a backup with:
  docker compose cp backups\<file>.sql db:/tmp/restore.sql
  docker compose exec -T db sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysql -uroot $MYSQL_DATABASE < /tmp/restore.sql'
#>
param([switch]$Yes)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)   # the backend folder (docker-compose.yml)

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

Step 'Backing up the database'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T db sh -c 'MYSQL_PWD=$MYSQL_ROOT_PASSWORD mysqldump -uroot --single-transaction --routines $MYSQL_DATABASE --result-file=/tmp/backup.sql'
Check 'Backup'
docker compose cp db:/tmp/backup.sql "backups/attendance-$stamp.sql"
Check 'Copying the backup out'
Write-Host "Saved backups\attendance-$stamp.sql"

Step 'Applying migrations'
docker compose exec -T web python manage.py migrate --noinput
Check 'Migrate'

Step 'Preview (nothing is changed yet)'
docker compose exec -T web python manage.py regenerate_attendance --dry-run
Check 'Preview'

if (-not $Yes) {
    # No console to answer from (piped / scheduled): never assume yes.
    if ([Console]::IsInputRedirected) {
        Write-Host 'No console to confirm from - re-run with -Yes to apply. Nothing was changed (migrations aside).'
        exit 1
    }
    [string]$answer = Read-Host "`nApply these changes? [y/N]"
    if (-not ($answer.Trim() -match '^(y|yes)$')) {
        Write-Host 'Stopped. Nothing was changed (migrations aside).'
        exit 0
    }
}

Step 'Regenerating attendance'
docker compose exec -T web python manage.py regenerate_attendance
Check 'Regenerate'

Step 'Restarting app containers'
docker compose restart web celery_worker celery_beat attendance_listener
Check 'Restart'

Write-Host "`nDone. Backup: backups\attendance-$stamp.sql" -ForegroundColor Green
