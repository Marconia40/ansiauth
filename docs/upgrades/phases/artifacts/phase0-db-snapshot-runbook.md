# Phase 0 T0.2 — DB snapshot runbook

**Status:** requires operator action. I cannot reach production or your local
Docker Postgres from this session.

## Why

Every subsequent MSP phase migration writes to production data. M4 (Phase 5)
is **not** cleanly reversible. A pre-MSP snapshot is the recovery path if any
phase misbehaves.

Take one snapshot **before Phase 1 deploys** and one more **immediately
before Phase 5 deploys** (the second one covers the point-of-no-return
window per phase-5-cleanup.md §Rollback).

## Commands to run

### Production Postgres (recommended)

Run from wherever you can reach the prod DB. Substitute the actual host /
DB name if it differs from the docker-compose default (`db:5432/ansiauth`,
user `ansiauth`).

```bash
# 1. Full logical dump (single file, easy to restore)
PGPASSWORD='<your_prod_password>' pg_dump \
    --host=<prod-host> \
    --username=ansiauth \
    --dbname=ansiauth \
    --format=plain --no-owner --no-privileges \
    > /backup/ansiauth_pre_msp_$(date +%F_%H%M).sql

# 2. Verify the dump loads on a scratch DB (do NOT run against prod)
createdb -h localhost -U ansiauth ansiauth_pre_msp_restore_test
psql -h localhost -U ansiauth -d ansiauth_pre_msp_restore_test \
     -f /backup/ansiauth_pre_msp_<timestamp>.sql
psql -h localhost -U ansiauth -d ansiauth_pre_msp_restore_test \
     -c 'SELECT COUNT(*) FROM devices;'
dropdb -h localhost -U ansiauth ansiauth_pre_msp_restore_test
```

### Local Docker Postgres (for pre-flight rehearsals)

```bash
docker compose exec db \
    pg_dump -U ansiauth -d ansiauth --format=plain --no-owner --no-privileges \
    > /backup/ansiauth_local_pre_msp_$(date +%F_%H%M).sql
```

### Local SQLite dev DB (already covered)

`backend/app/db/app.db` is a plain SQLite file — just copy it:

```bash
cp backend/app/db/app.db /backup/ansiauth_dev_pre_msp_$(date +%F_%H%M).sqlite
```

## Storage & retention

- Location: pick a directory outside the repo (`/backup/…` above is a
  placeholder). Do **not** commit backups.
- Retention: keep the pre-Phase-1 snapshot for at least **90 days** and the
  pre-Phase-5 snapshot for at least **30 days** after Phase 5 lands.
- Access control: SQL dumps contain hashed passwords, encrypted device
  credentials, JWT keys (if stored in DB), and audit history. Protect
  accordingly.

## What to record here after each snapshot

Append a line to this file after each snapshot:

```
YYYY-MM-DD HH:MM — pre-Phase-N — /backup/ansiauth_pre_msp_<ts>.sql — sha256:<hash>
```

For example:
```
# Snapshot log
2026-08-19 21:15 — pre-Phase-1 — /backup/ansiauth_pre_msp_2026-08-19_2115.sql — sha256:abc123…
```

## Restore procedure (for reference)

```bash
# 1. Stop app processes (backend + worker)
docker compose stop backend worker

# 2. Restore
dropdb   -h <host> -U ansiauth ansiauth
createdb -h <host> -U ansiauth ansiauth
psql     -h <host> -U ansiauth -d ansiauth -f /backup/ansiauth_pre_msp_<ts>.sql

# 3. Restart
docker compose start backend worker
```

---

# Snapshot log

<!-- Append one line per snapshot as they are taken. -->

2026-08-19 23:50 — pre-Phase-1 — ~/ansiauth-backups/ansiauth_pre_msp_2026-08-19_2350.sql — sha256:6e17f33bf03637dd8d6521569f7427e863d3e591eb5ad52e764c15bfa27915e1

2026-08-22 12:30 — pre-Phase-5 — ~/ansiauth-backups/ansiauth_pre_msp_2026-08-22_1230.sql — 
sha256:caacd8eac663bf83cbd737062fdeb0ad74352942d84b4bb7e960abf037fb425b
