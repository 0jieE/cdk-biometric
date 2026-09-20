# Docker Deployment (Phase 4)

Containerizes the **backend** stack — Django + Celery + Celery Beat + Redis +
MySQL + Nginx. The Flutter app stays outside Docker and points at the running
backend.

> **Note on the base image:** there is no official "Django" image. Django is a
> Python library, so the `web` image starts from `python:3.12-slim` and installs
> Django (and everything else) from `requirements.txt`.

## Prerequisites

- **Docker Desktop** running (provides Docker Engine + Compose v2 on Windows).
- Nothing else — **Redis and MySQL now run as containers**, so the Phase 2
  Memurai/WSL Redis workaround is no longer needed.

## Files

All of this lives at the repo root (`backend/`) alongside `manage.py`:

```
Dockerfile                    # python:3.12-slim + deps + entrypoint
entrypoint.sh                 # wait-for-db, migrate, seed, collectstatic, exec
.dockerignore
docker-compose.yml            # base, prod-shaped (gunicorn + nginx)
docker-compose.override.yml   # dev: bind mounts + autoreload + auto-seed
nginx/default.conf            # reverse proxy, serves /static/ + /media/
.env.docker                   # docker env (service-name hosts)
```

## Services

| Service         | Image / build           | Role                                             |
|-----------------|-------------------------|--------------------------------------------------|
| `db`            | `mysql:8`               | MySQL; data in named volume `mysql_data`         |
| `redis`         | `redis:7-alpine`        | Celery broker + result backend                   |
| `web`           | build `.` (this repo)   | Django (gunicorn in prod, runserver in dev)      |
| `celery_worker` | `biometric-backend`     | Celery worker                                    |
| `celery_beat`   | `biometric-backend`     | Periodic scheduler (DB-backed)                   |
| `nginx`         | `nginx:alpine`          | Reverse proxy on :80, serves static + media      |

---

## Quick start

From the repo root (`backend/`, where the compose files live):

```bash
docker compose build web
docker compose up -d
```

`celery_worker`, `celery_beat`, and `attendance_listener` all reuse the image
`web` builds (`image: biometric-backend:latest`, no `build:` of their own) —
building it explicitly first, as its own step, guarantees it exists locally
before anything tries to start, on every Compose version. Skipping straight to
`docker compose up --build` usually works too (recent Compose builds `web`
before starting anything else), but on some older Compose builds this can
lose the race and try to **pull** `biometric-backend` from Docker Hub instead
— which fails with `pull access denied` (it only exists locally, never
pushed anywhere). If you hit that, the two-step form above always fixes it.

This builds the `web` image and starts all six services. On **first boot** the
`web` entrypoint:

1. waits for MySQL to accept connections,
2. runs `migrate`,
3. runs `seed_data` (because `SEED_ON_START=true`) — idempotent, so re-runs are safe,
4. runs `collectstatic` (because `COLLECT_STATIC=true`).

`celery_worker` / `celery_beat` wait until the web service has applied migrations
before starting, so there's no migration race.

### URLs

- **Web admin portal (via nginx):** http://localhost/
- **Direct Django (dev only):** http://localhost:8001/
- **Mobile API:** http://localhost/api/v1/

### Seeded logins

| Role     | Username   | Password        |
|----------|------------|-----------------|
| Admin    | `admin`    | `admin12345`    |
| Employee | `emp-1001` | `employee12345` |

---

## The no-rebuild dev loop

`docker-compose.override.yml` is auto-merged by `docker compose up`. It
bind-mounts this whole repo into every Python container, so:

- Edit any `.py` → **`web` autoreloads** (Django `runserver`), no rebuild.
- The **Celery worker/beat auto-restart** via `watchmedo auto-restart` (from the
  `watchdog` package), picking up code + schedule changes.

**You only rebuild when `requirements.txt` changes:**

```bash
docker compose up --build            # or: docker compose build web
```

A plain restart always runs the newest bind-mounted code:

```bash
docker compose restart web celery_worker
```

**Windows / Docker Desktop notes (baked into the override):**

- The worker/beat run watchmedo with **`--debug-force-polling`** because inotify
  filesystem events don't cross a Windows bind mount — polling detects changes
  reliably. Django's `runserver` already polls (`StatReloader`), so `web` is fine.
- The dev `web` host port defaults to **`${WEB_PORT:-8001}`** (8000 is taken by
  another local project). If 8001 is also busy, pick another:

  ```bash
  WEB_PORT=8002 docker compose up -d      # direct Django on :8002; nginx still :80
  ```

  On PowerShell: `$env:WEB_PORT=8002; docker compose up -d`. **nginx on port 80 is
  the primary entry point** regardless — the direct port is just a dev convenience.
  This is the *host* port; the container always listens internally on 8000 (which
  nginx proxies to — don't change that).

---

## Data persistence

MySQL data lives in the named volume **`biometric_mysql_data`** (`/var/lib/mysql`).
It survives container recreation:

```bash
docker compose down          # stops & removes containers, KEEPS volumes
docker compose up            # data still there; seed is idempotent (no dupes)
```

To wipe everything **including the database** (destructive):

```bash
docker compose down -v       # also removes named volumes
```

---

## Common commands

```bash
# Start in the background
docker compose up -d --build

# Follow logs (all, or one service)
docker compose logs -f
docker compose logs -f web
docker compose logs -f celery_beat

# One-off management command inside the running web container
docker compose exec web python manage.py sync_attendance
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py shell

# Run the test suite in the container
docker compose exec web python manage.py test apps

# Open a shell
docker compose exec web bash

# Stop
docker compose down

# Rebuild after changing requirements.txt
docker compose build web && docker compose up -d
```

---

## Verifying it works

- `docker compose ps` → all services `Up` (db `healthy`).
- Open http://localhost/ → portal login; sign in as `admin` / `admin12345`.
- Dashboard shows seeded employees and punches.
- `docker compose logs -f celery_beat` → the periodic **"Sync attendance from
  device"** task fires every `SYNC_INTERVAL_MINUTES` (default 5), and
  `celery_worker` logs the sync running — mock data keeps flowing.

---

## Connecting the Flutter app (outside Docker)

The app talks to the backend over your **host LAN IP**, not `localhost`
(localhost on the phone is the phone itself).

1. Find your host IP (`ipconfig` → IPv4, e.g. `192.168.1.10`).
2. Add it to `ALLOWED_HOSTS` in `.env.docker`:
   ```
   ALLOWED_HOSTS=localhost,127.0.0.1,web,nginx,192.168.1.10
   ```
   then `docker compose up -d` (restarts web with the new value).
3. Point the Flutter app's API base URL at `http://192.168.1.10/api/v1/`
   (port 80 via nginx). Phone and PC must be on the same network.

---

## Switching to the real ZKTeco device

Development uses the mock backend. To use the physical unit:

1. In `.env.docker` set:
   ```
   BIOMETRIC_DEVICE_BACKEND=zk
   ZK_DEVICE_IP=<device-ip-on-your-LAN>
   ```
2. Restart the services that talk to the device:
   ```bash
   docker compose up -d web celery_worker
   ```

The device sits on the LAN; containers reach it by IP (Docker Desktop on Windows
routes LAN IPs fine). **If a container can't reach the device**, fall back to
either:

- running the sync worker on the host (host has direct LAN access):
  `python manage.py sync_attendance` from this repo's folder, or
- giving the worker host networking (Linux): add `network_mode: host` to
  `celery_worker` (note: host networking is limited on Docker Desktop for
  Windows/Mac; the host-run worker is the more reliable fallback there).

---

## HTTPS (optional, for the capstone)

`nginx/default.conf` contains a commented HTTPS server block with self-signed
cert instructions. Generate a cert, mount `./nginx/certs`, publish `443:443`,
and uncomment the block. Real certs/domain are optional for this project.

---

## Phase 5 — Put the API online (public tunnel)

Two tunnel options are wired up. **`ngrok` is the default/primary one** — it
starts with a plain `docker compose up -d`. `cloudflared` is kept as a fallback
behind a Compose **profile**, so it never starts unless you explicitly ask for
it (see why below).

### ngrok (default — free static domain, no rate-limit surprises)

Unlike the Cloudflare Quick Tunnel below, ngrok's free plan gives every account
**one permanent static domain** (e.g. `usage-angles-emporium.ngrok-free.dev`)
that never changes across restarts and isn't subject to the same anonymous
rate limiting.

**One-time account setup:**
1. Sign up free at [dashboard.ngrok.com/signup](https://dashboard.ngrok.com/signup).
2. Grab your authtoken from [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken).
3. Your static domain is auto-assigned on signup — find it under **Universal
   Gateway → Domains** in the dashboard (no need to type a custom name).

**In `.env.docker`:**
```
NGROK_AUTHTOKEN=<your authtoken>
NGROK_STATIC_DOMAIN=<your-domain>.ngrok-free.dev
```

**Bring it up:**
```bash
docker compose up -d            # starts the whole stack, ngrok included
```

Your public URL is always `https://<NGROK_STATIC_DOMAIN>` — no need to fetch
it from logs each time, since it never changes. To confirm it's actually
connected: `docker compose logs ngrok | grep "started tunnel"`.

### Alternative: Cloudflare Quick Tunnel (anonymous, rate-limited, opt-in)

A `cloudflared` service can instead give a **temporary** public
**`https://<random>.trycloudflare.com`** URL — no account, no domain, but the
URL changes on every restart and Cloudflare rate-limits anonymous quick
tunnels per IP if restarted too often. It's gated behind a profile so routine
`docker compose up -d` never touches it:

```bash
docker compose --profile cloudflared up -d cloudflared
docker compose logs cloudflared | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
```

```powershell
# PowerShell equivalent of the grep above
docker compose logs cloudflared | Select-String -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | ForEach-Object { $_.Matches.Value } | Select-Object -Last 1
```

If you hit `429`/error `1015` in its logs, that's Cloudflare's rate limit —
stop it (`docker compose stop cloudflared`) and wait 15–30 minutes before
starting it again; don't retry in a loop, each failed attempt still counts
against the limit.

### Point the Flutter app at whichever tunnel is up

The app reads `API_BASE_URL` (`--dart-define`) and appends `/api/v1` itself, so
pass the bare tunnel URL (no path, no trailing slash):

```bash
flutter run --dart-define=API_BASE_URL=https://<your-domain>.ngrok-free.dev
```

No `10.0.2.2` or LAN IP needed — the HTTPS tunnel works from the emulator, a
physical phone on mobile data, anywhere. Being real HTTPS, there's no Android
cleartext-traffic issue.

### Django settings that make this work

- `ALLOWED_HOSTS` includes **`.trycloudflare.com`**, **`.ngrok-free.app`**, and
  **`.ngrok-free.dev`** (leading dot ⇒ any subdomain), so neither tunnel's host
  ever triggers `DisallowedHost`.
- `CSRF_TRUSTED_ORIGINS` includes the `https://*.` form of all three, so the
  CSRF-protected **admin portal** POST forms work over either tunnel.
- `SECURE_PROXY_SSL_HEADER` + `USE_X_FORWARDED_HOST` — both tunnels terminate
  TLS and forward to nginx over http, so Django trusts `X-Forwarded-Proto:
  https` (nginx already forwards it).
- The DRF JSON API (mobile app) is token-based and **not** subject to CSRF, so it
  works regardless of the CSRF settings.

### ⚠️ Caveats (read these)

- The backend is reachable from the internet while a tunnel is up. **Change the
  seeded default admin password** before exposing it, and stop the tunnel when
  done (`docker compose stop ngrok`, or `docker compose stop cloudflared`).
- With `cloudflared`: don't restart it repeatedly (each restart requests a new
  random URL and burns the anonymous rate limit) — see above.
- With `ngrok`: the free plan's one static domain is tied to your account, not
  to this specific deployment — if you run the stack on a second machine with
  the same `NGROK_AUTHTOKEN`, only one can hold an active session at a time.

### Definition of Done — Phase 5 (verified)

| # | Criterion | Status |
|---|-----------|--------|
| 1 | `docker compose up -d` starts the Phase 4 stack **plus** `cloudflared`     | ✅ |
| 2 | Logs / helper print a `https://<random>.trycloudflare.com` URL             | ✅ |
| 3 | That URL loads the portal over HTTPS through nginx (login page)            | ✅ HTTP 200 |
| 4 | Flutter app (`--dart-define=API_BASE_URL=<url>`) logs in over the internet | ▶ run on device |
| 5 | No `DisallowedHost`/CSRF errors; admin portal login works over the tunnel   | ✅ CSRF login → 302 |
| 6 | Phase 4 still runs; stopping `cloudflared` leaves the local stack intact    | ✅ |

---

## Phase 7 — FCM push notifications (backend)

Real Firebase Cloud Messaging sending is enabled by mounting a service-account
key and pointing `FIREBASE_CREDENTIALS` at it.

- **Service-account key (secret):** `secrets/firebase-service-account.json`
  — gitignored (`.gitignore` + `.dockerignore`), never committed, never baked
  into the image. The dev bind mount exposes it at
  `/app/secrets/firebase-service-account.json` inside the containers.
- **Env:** `.env.docker` → `FIREBASE_CREDENTIALS=/app/secrets/firebase-service-account.json`
- **Firebase project:** `biometric-attendance-25729`

After adding the key / editing `.env.docker`, **recreate** (not just restart —
env_file is read at container-create) the affected services:

```bash
docker compose up -d web celery_worker
```

`apps/notifications/fcm.py` initializes firebase-admin on first send and logs
`FCM enabled (project=…)`; it prunes dead tokens (`UnregisteredError` /
`SenderIdMismatchError` / malformed) by deactivating the `MobileDevice`.

**Send a test push** (employee must have registered a device via the app first):

```bash
docker compose exec web python manage.py send_test_push EMP-1001
docker compose exec web python manage.py send_test_push EMP-1001 --title "Hi" --body "..."
```

With no registered device it reports "FCM is enabled but the employee has no
registered device" (created the notification, nothing to deliver to yet).

---

## Phase 8 — Attendance model & new pages

New admin pages (ADMIN-gated, in the sidebar): **`/schedule/`** (global default +
per-employee overrides), **`/overtime/`** (grant/revoke OT authorizations — OT is
computed only when authorized), **`/manual-attendance/`** (brownout fallback;
records `source=MANUAL` + `created_by`).

**`/live/` is a PUBLIC kiosk page (no login)** showing recent punches
(name, punch, time, department), auto-refreshing every 15s.

> ⚠️ **Privacy:** `/live/` is reachable by anyone with the URL — **via the
> Cloudflare tunnel that is the public internet**, exposing employee names. It's
> deliberately minimal (no filters, no IDs). To disable it entirely set
> `LIVE_LOGS_PUBLIC=False` in `.env.docker` (the page then returns 404). For a
> middle ground, keep it off the tunnel (LAN-only) or move it behind an
> unguessable path. Default is `True`.

---

## Phase 10 — Real-time per-punch notifications

Every new attendance log (device sync, live capture, or manual entry) fires **one
immediate notification** the moment it's persisted — decoupled from the daily
late/absent batch. Each notification links to its log (`Notification.attendance_log`,
unique) so re-syncing never duplicates.

- **Message** names the punch + local time, e.g. `AM In recorded — 8:02 AM`;
  a late session-IN includes the exact minutes: `AM In — 8:12 AM (Late by 7 min)`.
- **Delivery** is immediate; `send_fcm_notification_task` retries **only on
  transient failure** (backoff to 10 min, up to 24 tries) and **prunes** dead
  tokens (`UnregisteredError`) instead of retrying.
- **Outage recovery:** `retry_pending_notifications_task` runs on Celery Beat
  **every 2 minutes**, re-enqueuing anything still `PENDING`/unsent — so punches
  captured while the worker/host was down get pushed the moment it recovers.
- **Lateness** rides on the immediate punch push; the daily task still flags/
  notifies **absences** only (no double-send).

### Try it

```bash
# 1) Immediate push: create a manual punch in the portal (/manual-attendance/)
#    or ingest one — a linked notification is created and sent right away.
docker compose exec web python manage.py shell   # ingest a RawPunch -> see STATUS=SENT

# 2) Outage recovery: stop the worker, create a punch, then restart —
#    the sweeper (or the queued task) delivers it on recovery.
docker compose stop celery_worker
#   ...create a punch (manual-attendance page)...
docker compose start celery_worker              # delivered within ~2 min by the sweeper
docker compose logs -f celery_worker            # watch notifications.send_fcm run
```

### Beat schedule (seeded)

| Task                              | Every   |
|-----------------------------------|---------|
| `attendance.sync_attendance`      | 5 min   |
| `notifications.retry_pending`     | 2 min   |

Both are editable in Django admin → *Periodic tasks*.

### True real-time from hardware

Polling (`sync_attendance` on Beat) makes device punches *near*-real-time
(bounded by the interval); manual punches are instant. For **truly instant**
biometric punches when the ZKTeco unit is connected, run the streaming listener
(pyzk `live_capture`) as its own long-lived process:

```bash
# Requires BIOMETRIC_DEVICE_BACKEND=zk and a reachable unit.
docker compose exec web python manage.py attendance_listener
```

On the mock backend it reports that polling is used and exits.

---

## Definition of Done — Phase 4

| # | Criterion                                                              | How to check |
|---|------------------------------------------------------------------------|--------------|
| 1 | `docker compose up --build` starts all 6 services with no errors        | `docker compose ps` |
| 2 | First boot waits for MySQL, migrates, and auto-seeds                     | `docker compose logs web` |
| 3 | Portal reachable via nginx at `http://localhost/`; static + media served | browser |
| 4 | Editing a `.py` reflects without rebuild (web reload + worker restart)   | edit + watch logs |
| 5 | `down` then `up` keeps all data (named volume); no seed duplication      | re-login, counts unchanged |
| 6 | Celery Beat runs the mock sync on schedule inside the container          | `docker compose logs -f celery_beat` |
| 7 | Flutter app logs in against the containerized backend via host LAN IP    | app login |
| 8 | This README documents startup, logs, one-off commands, rebuild, ZK swap  | — |
