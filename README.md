# Biometric Attendance Monitoring System — Backend (Phase 1)

Django + DRF backend for a Web-Based Biometric Attendance Monitoring System
(Colegio de Kidapawan). Phase 1 delivers the data model, role-based auth, the
attendance business logic, a **pluggable biometric-device layer** (real ZKTeco +
mock simulator), seed data, and the employee mobile JSON API.

> Phases 2–4 (HTMX dashboard, Celery, Flutter app, Docker/Nginx) are **not** in
> this phase, but the models/fields they need (FCM tokens, notifications) are
> already scaffolded so we don't migrate twice.

---

## Stack

- Python 3.13 + **Django 5.2 LTS**
- Django REST Framework + `djangorestframework-simplejwt` (JWT for the app)
- **MySQL** via `mysqlclient`
- `django-environ`, `django-cors-headers`
- `pyzk` (real ZKTeco device — used only behind the device factory)
- Timezone: **Asia/Manila**, `USE_TZ = True`

> **Deviation from the original prompt:** the prompt pinned Django 5.x / Python
> 3.12, but the provided virtualenv shipped Python 3.13 + Django 6.0. Python is
> kept at 3.13 (the venv's interpreter); Django is pinned to **5.2 LTS** as
> requested, for guaranteed DRF/simplejwt compatibility.

---

## Project layout

```
backend/
  core/            # project package (settings, urls, wsgi, asgi)
  env/             # virtualenv (git-ignored)
  apps/
    accounts/      # custom User + RBAC
    organization/  # Department, Employee, AttendanceConfig, Holiday (+ seed_data)
    devices/       # BiometricDevice, MobileDevice + clients/ (base, zk, mock, factory)
    attendance/    # AttendanceLog, Lates + services.py (+ sync_attendance command)
    notifications/ # Notification (model only in Phase 1)
    api/           # DRF serializers, permissions, selectors, views, urls
  requirements.txt
  .env / .env.example
```

---

## Setup & run

All commands are run from `backend/`. On Windows the venv Python is
`env\Scripts\python.exe` (examples below use the `env/Scripts/python.exe` form
that works in Git Bash / PowerShell).

### 1. Install dependencies

```bash
env/Scripts/python.exe -m pip install -r requirements.txt
```

### 2. Configure environment

`.env` already contains working dev values (see `.env.example` for the template).
The MySQL database `attendance_db` must exist and be reachable with the
credentials in `.env` (defaults: user `root`, password `root`, `127.0.0.1:3306`).

### 3. Migrate

```bash
env/Scripts/python.exe manage.py migrate
```

### 4. Seed development data (idempotent)

```bash
env/Scripts/python.exe manage.py seed_data
```

Creates 3 departments, 10 demo employees (each with an EMPLOYEE user and a
`biometric_id`), the `admin` superuser, one `BiometricDevice`, the two
fixed-date national holidays, and the periodic sync tasks. Prints the admin + a
sample employee credential.

It deliberately seeds **nothing that changes how real punches are counted** — no
invented holidays, no overtime authorizations. (It runs on every container start,
and an older version added a fake "Foundation Day" holiday for every month it ran
in, plus an example OT authorization for every day it ran on. If your database
was seeded by that version, clean it once — see *Attendance data repair* below.)

### 5. Sync attendance from the mock device

```bash
env/Scripts/python.exe manage.py sync_attendance            # last 7 days
env/Scripts/python.exe manage.py sync_attendance --days 14
env/Scripts/python.exe manage.py sync_attendance --since 2026-07-01
```

With `BIOMETRIC_DEVICE_BACKEND=mock` this fabricates realistic IN/OUT punches,
pairs them into `AttendanceLog` rows, and produces `Lates` rows. Re-running is
safe — duplicates are rejected by the unique constraint.

### 6. Run the server

```bash
env/Scripts/python.exe manage.py runserver
```

- Django admin (ADMIN only): http://127.0.0.1:8000/admin/
- Mobile API base: http://127.0.0.1:8000/api/v1/

### 7. Tests

```bash
env/Scripts/python.exe manage.py test apps
```

---

## Seeded credentials

| Role     | Username   | Password        | Where           |
|----------|------------|-----------------|-----------------|
| Admin    | `admin`    | `admin12345`    | Django admin    |
| Employee | `emp-1001` | `employee12345` | Mobile API      |

> The admin password comes from `ADMIN_PASSWORD` in `.env`. All 8 seeded
> employees (`emp-1001` … `emp-1008`) share the password `employee12345`.
> Employees whose `biometric_id` is divisible by 4 (`emp-1004`, `emp-1008`) are
> deterministically **late** in the mock, so `LATE` status is always testable.

---

## Mobile API (`/api/v1/`)

All employee endpoints require a JWT (`Authorization: Bearer <access>`) and
return **only the requesting employee's own data**.

| Method | Path                          | Purpose                                                   |
|--------|-------------------------------|-----------------------------------------------------------|
| GET    | `/health/`                    | Public liveness check (no auth)                           |
| POST   | `/auth/login/`                | Obtain access + refresh tokens                            |
| POST   | `/auth/refresh/`              | Refresh an access token                                   |
| GET    | `/me/`                        | Employee profile, incl. `photo_url` (absolute URL or `null`) |
| POST   | `/me/photo/`                  | Upload/replace the profile photo (`multipart/form-data`, field `photo`; `PUT` also works) |
| POST   | `/me/password/`               | Change password (`old_password`, `new_password`); returns fresh tokens |
| GET    | `/attendance/?start=&end=`    | Per-day record (AM/PM sessions, or IN/OUT for part-time) with `day_status` `PRESENT`/`LATE`/`HALF_DAY`/`ABSENT` |
| GET    | `/attendance/summary/?month=` | Monthly present / late / half-day / absent counts + late/undertime/lost/overtime minutes (`month=YYYY-MM`) |
| GET    | `/notifications/`             | The employee's notifications                              |
| POST   | `/devices/register/`          | Register/update an FCM token (`fcm_token`, `platform`)    |

`/attendance/` and `/notifications/` are paginated (`PAGE_SIZE = 25`).

### Profile photo

`GET /me/` returns `photo_url` — an absolute URL (https through the tunnel) the app
can load directly (`Image.network(url)`), or `null` if the employee has no photo. The
URL changes on every upload, so it never serves a stale picture. Photos are public
by URL, like the portal's other employee photos (the filename carries a random
suffix, so it isn't guessable).

`POST /me/photo/` takes `multipart/form-data` with a `photo` file and returns
`{"photo_url": ...}`. JPEG, PNG or WebP up to 10 MB (HEIC is not supported — have
the picker convert to JPEG). The server validates it is really an image, applies the
phone's rotation, shrinks it to at most 512 px, and re-encodes it as a JPEG with
**all metadata removed** (phone photos carry the GPS location they were taken at).
The previous photo file is deleted. Errors are `400 {"photo": ["..."]}`; JSON bodies
get `415`. A photo set in the web portal shows up in the same field.

### Changing the password

`POST /me/password/` with `{"old_password": "...", "new_password": "..."}`.
`400` with `old_password: ["Incorrect password."]` if the current one is wrong, or
`new_password: [...]` listing every rule it breaks (min length 8, not too common,
not all digits, not too similar to the username, not the same as before). Limited to
5 attempts a minute (`429` after that).

On success it returns `{"detail", "access", "refresh"}` — **store the new tokens**.
Every token issued before the change stops working: other devices get `401` on their
next call, and `POST /auth/refresh/` answers `401 {"code": "password_changed"}` — send
the user to the login screen. The same happens when an admin resets the password in
the portal.

> One-off effect of enabling this: tokens issued before it was turned on don't carry
> the password marker, so each phone signs in once more.

**How a day's status is decided:** from the punches themselves — a session (or a
part-time day) counts only with **both** an IN and an OUT. A workday with no
punches is `ABSENT`; it is never assumed `PRESENT`. Days before attendance
tracking began (see `ATTENDANCE_START_DATE`) are simply not reported, and
weekends/holidays are skipped.

### Quick example

```bash
# Login
curl -X POST http://127.0.0.1:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"username":"emp-1001","password":"employee12345"}'

# Use the returned access token
curl http://127.0.0.1:8000/api/v1/attendance/ \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

**Cross-employee access is impossible by construction:** no endpoint accepts an
employee id — every query is scoped to `request.user.employee`, and the
`IsEmployeeUser` permission blocks admins/unlinked accounts.

---

## Switching from the mock to a real ZKTeco device

The device layer is fully pluggable. Nothing imports a concrete client — code
always calls `apps.devices.clients.get_device_client()`, which reads
`BIOMETRIC_DEVICE_BACKEND`.

To go live on real hardware, change **one env variable** in `.env`:

```diff
- BIOMETRIC_DEVICE_BACKEND=mock
+ BIOMETRIC_DEVICE_BACKEND=zk
```

and point the `ZK_DEVICE_*` values at your unit:

```
ZK_DEVICE_IP=192.168.1.201
ZK_DEVICE_PORT=4370
ZK_DEVICE_TIMEOUT=10
```

Then `python manage.py sync_attendance` pulls from the physical device instead of
the simulator. No code changes required.

---

## Definition of Done — status

| # | Criterion                                                             | Status |
|---|-----------------------------------------------------------------------|--------|
| 1 | `pip install -r requirements.txt`; migrations run clean on MySQL      | ✅ |
| 2 | `seed_data` populates the DB and prints the admin login               | ✅ |
| 3 | `sync_attendance` (mock) creates AttendanceLogs and Lates rows        | ✅ |
| 4 | Django admin shows all models with list/filter/search                 | ✅ |
| 5 | Login returns JWTs; `/attendance/` returns paired logs with status    | ✅ |
| 6 | An employee token cannot access another employee's records            | ✅ |
| 7 | Tests pass (`manage.py test apps` → 16 passing)                       | ✅ |

---

## Notes / follow-ups

- **ERD deviation (intentional):** the paper linked `Attendance_Log` to
  `Mobile_Device`. Punches come from the **biometric** unit, so `AttendanceLog`
  is routed through `BiometricDevice`; `MobileDevice` is kept for FCM /
  notifications only. Say the word if you'd rather match the doc literally.
- For production, set a strong `SECRET_KEY` (the dev value triggers a short-key
  JWT warning) and `DEBUG=False`.

---
---

# PHASE 2 — Async + Web Admin Portal

Phase 2 adds Celery + Redis background processing, an HTMX admin web portal,
Excel/PDF report generation, and FCM push notifications (no-op until Firebase is
configured).

## New dependencies

`celery`, `redis`, `django-celery-beat`, `openpyxl`, `xhtml2pdf`, `firebase-admin`
(all in `requirements.txt`). Install with `pip install -r requirements.txt`.

## New env keys (already in `.env` / `.env.example`)

```
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
SYNC_INTERVAL_MINUTES=5
FIREBASE_CREDENTIALS=            # blank => FCM disabled (system still runs)
```

## ⚠️ Redis on Windows

Redis has no native Windows build. Pick one (the app code is identical either way):

- **Memurai** (drop-in Redis for Windows) — https://www.memurai.com/ , runs as a service on `6379`.
- **Docker**: `docker run -p 6379:6379 redis`
- **WSL2**: `sudo apt install redis-server && redis-server`

Verify it's up: `redis-cli ping` → `PONG` (or Memurai's `memurai-cli ping`).

## Running Phase 2 — three terminals

All from `backend/`, with the venv active. **Celery on Windows must use the solo pool.**

```bash
# Terminal 1 — Celery worker (solo pool required on Windows)
celery -A core worker -l info --pool=solo

# Terminal 2 — Celery Beat (periodic sync scheduler)
celery -A core beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

# Terminal 3 — Django dev server
python manage.py runserver
```

`seed_data` registers the periodic task **“Sync attendance from device”** that
Beat fires every `SYNC_INTERVAL_MINUTES`; it calls the Phase 1 mock sync so data
keeps flowing. You can enable/disable/retune it from Django admin
(*Periodic tasks*).

> **Dev without Redis:** the portal still fully works. "Sync now" and report
> generation fall back to running **inline** when no broker is reachable (the
> broker is configured to fail fast). Redis + a worker are only needed for the
> *background/scheduled* path (DoD #1–2).

## Web admin portal

- URL: **http://127.0.0.1:8000/**  (this is the portal login, not Django admin)
- Login: **admin / admin12345** (ADMIN role only; employees are redirected away)
- Pages: Dashboard (cards + auto-refreshing recent punches + "Sync now"),
  Attendance, Tardiness, Absences, Employees (CRUD + auto-provisions login +
  schedule), Departments, Holidays, Devices (per-device Sync / Test), Reports
  (async jobs with polling + download). All filtering, pagination, modals, and
  inline actions use **HTMX partial swaps** (Bootstrap 5 + HTMX via CDN).

## Reports

Five report types (Daily Attendance, Monthly Summary, Tardiness, Absence,
**Employee Attendance**), each in **Excel** (styled/frozen header, auto-width) and
**PDF** (institution header + timestamp). Files land in `MEDIA_ROOT/reports/` and
are served at `/media/` in dev. List pages have direct **Export PDF/Excel**
buttons; the Reports page runs generation **async** via Celery and shows job
status (and, for per-employee jobs, which employee) with a download link.

**Reports by employee.** Pick an employee on the Reports page:

- **Employee Attendance (DTR)** — one person's daily time record over a date
  range (default: this month, max 366 days). Lists *every* calendar day: punches
  and status for worked days, plus `REST` (non-workday), `HOLIDAY` (with its name),
  `Not tracked` (before attendance tracking began) and `Upcoming`; ends with a
  TOTAL row (days present/late/half-day/absent and late / undertime / OT / lost
  minutes). Full-time staff get AM/PM columns, part-time get Time In / Time Out.
  The file is named `employee_attendance_<employee no>_<timestamp>`. Inactive
  employees can still be reported (e.g. someone who has left).
- **Any of the other four reports** can also be limited to one person by choosing
  an employee (it overrides the department filter). The same `employee=<id>` query
  parameter works on the direct `/export/` endpoint.

## FCM

`apps/notifications/fcm.py` initialises `firebase-admin` from
`FIREBASE_CREDENTIALS`. **Blank ⇒ it logs "FCM disabled" and no-ops** — the whole
system runs without Firebase. `process_daily_attendance_task` still creates
`Notification` rows for LATE/ABSENT and enqueues delivery; sending activates the
moment a service-account JSON path is provided. Dead tokens are pruned on
`UnregisteredError`.

## New models

- `attendance.Absence` (employee, date, reason=`NO_PUNCH`) — created idempotently
  by `process_daily_attendance_task`, skips holidays/non-workdays.
- `reports.ReportJob` — tracks async report generation (type, fmt, params,
  status, file, requested_by, timestamps).

## Definition of Done — Phase 2

| # | Criterion                                                              | Status |
|---|------------------------------------------------------------------------|--------|
| 1 | Worker + Beat start without errors (Redis up)                          | ✅ (needs Redis) |
| 2 | Beat auto-runs sync every `SYNC_INTERVAL_MINUTES`; mock data flows      | ✅ (periodic task seeded) |
| 3 | Admin sees dashboard cards + recent punches; "Sync now" works           | ✅ |
| 4 | Attendance/Tardiness/Absence filter via HTMX; export Excel + PDF        | ✅ |
| 5 | Employee/Dept/Holiday/Device CRUD via HTMX modals; employee create      | ✅ |
|   | also provisions login + config                                         |    |
| 6 | Report submitted from `/reports/` runs async, shows status, downloads   | ✅ |
| 7 | With `FIREBASE_CREDENTIALS` blank, everything runs; FCM no-ops cleanly   | ✅ |
| 8 | Tests pass (`manage.py test apps` → **29 passing**)                     | ✅ |

## Tests

```bash
python manage.py test apps
```

Covers (Phase 2): `process_daily_attendance_task` absence flagging + holiday /
non-workday skips + idempotency; report generators produce valid non-empty XLSX
(`PK…`) and PDF (`%PDF…`); portal RBAC (employee denied, admin allowed); FCM
`send_to_employee` no-ops when unconfigured.

---

# Attendance data repair

Derived rows (absences, lates, undertime, overtime) are created live, day by
day. Two management commands repair a database where that went wrong; both are
safe to re-run.

```bash
# 1. One-time: remove fake rows an OLD seed_data left behind (dry run first).
python manage.py cleanup_seed_artifacts --dry-run
python manage.py cleanup_seed_artifacts

# 2. Recompute absences etc. for days the stack was down (tracking start -> yesterday).
python manage.py backfill_attendance --dry-run
python manage.py backfill_attendance
python manage.py backfill_attendance --since 2026-09-14 --until 2026-09-20   # explicit range
```

`backfill_attendance` never sends push notifications about old days. Run it any
time after an outage. In Docker: `docker compose exec web python manage.py ...`.

**`ATTENDANCE_START_DATE`** (optional env, `YYYY-MM-DD`) — the first day real
attendance was recorded. Days before it are never reported present/absent or
backfilled. Left blank it defaults to the day of the earliest recorded punch, so
it usually needs no setting; set it if a stray early test punch would otherwise
move the start too far back. Days that have real punches always show.

---

# Demo data for testing reports

To test reports (and the mobile app) against a full, varied month, seed **fake**
attendance for every employee:

```bash
python manage.py seed_demo_attendance                    # this month, through yesterday
python manage.py seed_demo_attendance --month 2026-09    # a specific month
python manage.py seed_demo_attendance --month 2026-09 --clear   # remove it again
```

It is **opt-in** (nothing runs it automatically) and built so it can't be mistaken
for, or damage, real records:

- Every punch is tagged `source=DEMO` (shown as *demo* on the dashboard) and every
  overtime authorization it creates has the note `Demo data`.
- It **never sends notifications** — no push to real phones about fake punches.
- It **never touches an employee-day that already has a real punch**, or one with
  an admin-created overtime authorization.
- Re-running **replaces** its own rows (no duplicates). Only completed days are
  seeded (through yesterday) — never punches in the future.
- `--clear` removes the demo rows and re-derives the real period's absences, so
  the database ends up exactly as it would have been without the seed.

The patterns come from the mock device simulator and run through the *same*
classification code real punches use: EMP-1004 / EMP-1008 are chronically late,
EMP-1005 / EMP-1010 work authorized overtime, EMP-1001 / EMP-1006 leave late with
no authorization (so no overtime is computed), and everyone gets occasional
half-days and absences; part-timers get occasional single-punch (incomplete) days.
The output is deterministic, so two runs — or two machines — produce the same data.

**Heads-up:** the earliest punch defines when attendance tracking began (see
`ATTENDANCE_START_DATE`), so seeding a month makes that month "tracked": days with
no punches then read as absences. `--clear` restores the previous start. Today is
never seeded, so it shows as absent until real punches arrive.
