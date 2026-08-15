# Runbook — Rotating `JWT_SECRET_KEY`

The `JWT_SECRET_KEY` env var signs every short-lived access token. Rotating it
is a small, well-scoped operation but it has one user-facing consequence
you must communicate before running it.

## What rotation actually does

- **Access tokens (JWT, ~20 min lifetime by default):** every currently-issued
  token becomes immediately invalid. Browsers/clients will start getting
  `401 Invalid or expired token` on their next API call.
- **Refresh tokens (random opaque string, ~4 h lifetime, stored hashed in
  `refresh_tokens.token_hash`):** **unaffected**. The refresh-token rotation
  path doesn't sign anything with `JWT_SECRET_KEY` — it just looks up a SHA-256
  hash. So a user with a valid refresh cookie will silently get a new access
  token on their next `/auth/refresh` call without re-entering credentials.

Net effect for end-users: any open browser tab makes one extra round-trip on
the next action, then continues. CLI scripts that pasted a raw access token
will need to re-login.

## When to rotate

- **Routinely:** every 90 days, or whenever your secret-management policy
  says so.
- **Immediately:** if you suspect the secret leaked (committed to git,
  exposed in a log, copied to the wrong host, …).

## Prerequisites

- Access to whatever stores the env var (`.env`, Vault, AWS Secrets Manager,
  a Kubernetes secret, …).
- Ability to restart the backend process (or all backend pods).
- A maintenance window of <1 minute (the restart itself is the only outage).

## Procedure

### 1. Generate a new secret

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output. It's a 64-character hex string (32 random bytes).

### 2. Stage the new value in your secret store

Update wherever `JWT_SECRET_KEY` is read from — for example:

| Deployment style          | Where to update                                  |
|---------------------------|--------------------------------------------------|
| Bare metal / VM with `.env`| `backend/.env` (do **not** commit)               |
| docker-compose            | root `.env` consumed by `${JWT_SECRET_KEY}` in compose |
| Kubernetes                | the `Secret` object referenced by the Deployment |

Do *not* update the running backend's environment yet — set it for the
next process start.

### 3. Restart the backend

```bash
# docker-compose
docker compose restart backend

# systemd
sudo systemctl restart ansiauth-backend

# Kubernetes
kubectl rollout restart deployment/ansiauth-backend
```

The backend re-reads `JWT_SECRET_KEY` once at import time
(`app.core.config.Settings`), so a restart is required.

### 4. Monitor

For the first 5–10 minutes after the restart, watch:

- A **brief spike in `401 Invalid or expired token`** on `/api/v1/*` endpoints
  — expected and self-healing. Affected clients will refresh on their next
  call.
- **`token_refresh` audit events** should follow within seconds — confirms
  refresh tokens still work.
- **No spike in `/auth/login` failures** — would indicate refresh-token
  rotation isn't working. Investigate immediately.

If 401s persist past ~30 minutes (longer than the refresh-token lifetime),
some clients aren't refreshing — usually CLI scripts that don't implement
the refresh dance. They need to re-login.

### 5. Confirm and document

- Make a note in your change log: who rotated, when, why.
- If rotation was triggered by a suspected leak, also revoke any leaked
  credentials separately (e.g. force-revoke active refresh tokens with
  `DELETE FROM refresh_tokens WHERE revoked = FALSE`).

## Rollback

If something goes badly wrong (e.g. the new secret was mistyped and *no*
JWTs validate), restore the previous value in the secret store and restart
the backend again. There's no DB state to roll back — JWT signing is stateless.

## What does **not** require rotation

- The Fernet key (`FERNET_KEY`) — that's a separate runbook
  (`fernet-key-rotation.md`) because rotating it without re-wrapping device
  passwords will break every device login.
- The bootstrap admin password — once a real super-admin exists in the DB,
  `BOOTSTRAP_ADMIN_PASSWORD` is ignored on subsequent boots.
