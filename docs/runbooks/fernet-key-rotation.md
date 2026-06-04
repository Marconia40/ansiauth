# Runbook — Rotating `FERNET_KEY`

`FERNET_KEY` wraps every device credential at rest (column
`devices.encrypted_password`). Rotating it is **not** stateless — old
ciphertexts cannot be decrypted with the new key. You must re-wrap every
row **before** swapping the env var, otherwise device logins start failing.

The supplied `tools/rewrap-passwords.py` does the re-wrap step.

## What rotation actually does

- **Existing ciphertexts** in `devices.encrypted_password` are bound to the
  current key. If you change `FERNET_KEY` without re-wrapping them, every
  playbook that needs a device credential will raise
  `cryptography.fernet.InvalidToken` on decrypt.
- **New ciphertexts** (created via `POST /api/v1/devices/`) start using
  whatever `FERNET_KEY` is set when the backend boots.

## When to rotate

- **Routinely:** every 12 months, or per your secret-management policy.
- **Immediately:** if the key leaked, or if a privileged operator with key
  access has left the org.

## Prerequisites

- The current `FERNET_KEY` value (you'll need it as `OLD_FERNET_KEY`).
- A new key generated with `cryptography`'s helper (see step 1).
- Read/write access to the production database with the `DATABASE_URL` the
  tool can use.
- Python 3.10+ on the machine running the tool, with `cryptography` and
  `sqlalchemy` available (the regular backend requirements cover this).
- A maintenance window of **the time the rewrap takes** (~seconds for
  hundreds of devices, single-digit minutes for thousands) **plus a backend
  restart**.

## Procedure

### 1. Generate a new key

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Output is a 44-character URL-safe base64 string. Save it somewhere secure —
you'll need it as `NEW_FERNET_KEY` *and* as the new value of `FERNET_KEY`
once re-wrap completes.

### 2. Verify the tool sees every device (dry-run)

The script defaults to acting on `devices.encrypted_password`. A dry-run
decrypts and re-encrypts in memory but rolls back instead of committing —
useful to confirm the old key actually decrypts every row before you commit
to the rotation.

```bash
export OLD_FERNET_KEY="<current FERNET_KEY value>"
export NEW_FERNET_KEY="<output from step 1>"
export DATABASE_URL="<your production DB URL>"

python3 tools/rewrap-passwords.py --dry-run
```

Expected output:
```
info: scanning N row(s) in devices.encrypted_password
rewrap-passwords: table=devices column=encrypted_password processed=N skipped=0 errors=0 (DRY RUN — rolled back)
```

If `errors > 0`, the old key doesn't match every ciphertext — stop and
investigate **before** touching anything else. Common causes:

- The supplied `OLD_FERNET_KEY` is stale (someone already rotated and the
  env var you copied is out of date).
- Some rows were inserted under a *different* key at some point — these
  show up as `decrypt failed (wrong key or already rotated)`. You'll need
  to know which key wrapped them and rewrap-piecewise.

### 3. Run the rewrap for real

```bash
python3 tools/rewrap-passwords.py
```

The script runs everything in a single transaction. On success:
```
rewrap-passwords: table=devices column=encrypted_password processed=N skipped=0 errors=0
```
Exit code is 0.

If anything fails mid-flight, the transaction rolls back — every row is
still readable with the **old** key. Re-investigate, then re-run.

### 4. Swap the env var and restart the backend

Now (and only now) update the secret store:

| Deployment style          | Where to update                            |
|---------------------------|--------------------------------------------|
| Bare metal / VM with `.env`| `backend/.env`                            |
| docker-compose            | root `.env` consumed by `${FERNET_KEY}` in compose |
| Kubernetes                | the `Secret` object referenced by the Deployment  |

Then restart:

```bash
# docker-compose
docker compose restart backend
# systemd
sudo systemctl restart ansiauth-backend
# Kubernetes
kubectl rollout restart deployment/ansiauth-backend
```

### 5. Smoke-test a single device

Trigger any operation that decrypts a device credential (e.g.
`POST /api/v1/vlans/` with a single device). Confirm:

- Job completes with `status=completed`.
- No `InvalidToken` in the backend logs.

### 6. Securely destroy the old key

Now that no DB row references it, the old key is no longer needed. Remove
it from your secret store, password manager, and any pasted notes. If you
keep an archived copy "just in case", treat it with the same care as the
live key — a leaked old key still discloses every credential that ever
existed in the system up to the rotation timestamp (because git history,
backups, replicas all retain old ciphertexts).

## Rollback

If something goes wrong **after** step 4 (the env-var swap):

- If you haven't destroyed the old key yet: restore the old `FERNET_KEY`
  value, restart the backend. Every row is still decryptable under it
  because step 3 wrote new ciphertexts; you'd need to re-run the rewrap in
  reverse (`--old-key <NEW>` / `--new-key <OLD>`).
- If you have destroyed the old key: you must reset every device password
  via your network team. There's no other recovery path.

This is why step 6 ("destroy the old key") sits at the end, after a
real smoke test confirms the new key works.

## Reference

- Script source: `tools/rewrap-passwords.py`
- Decryption / re-encryption logic: `backend/app/services/secret_service.py`
- Encrypted column: `devices.encrypted_password`
