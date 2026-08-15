#!/usr/bin/env python3
"""Re-wrap every encrypted device credential under a new Fernet key.

Standalone CLI — not part of the app. Doesn't import any FastAPI / SQLAlchemy
ORM models so it can run in environments where the backend isn't installed
(e.g. a one-off pod in a deploy pipeline). Reads credentials with the *old*
Fernet key, writes them back with the *new* one, in a single transaction.

Inputs (CLI args win over env vars):
  --old-key / OLD_FERNET_KEY  — current Fernet key (base64-encoded)
  --new-key / NEW_FERNET_KEY  — replacement Fernet key (base64-encoded)
  --database-url / DATABASE_URL — SQLAlchemy URL of the target DB
  --table         (default: ``devices``)
  --column        (default: ``encrypted_password``)
  --dry-run       Decrypt + re-encrypt in memory but don't COMMIT.

Exits non-zero on any error so the script is safe to use in a scripted deploy
(`set -e` will halt the rollout). Prints a one-line summary on success.

See ``docs/runbooks/fernet-key-rotation.md`` for the operator workflow.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine, text


def _resolve(arg_value: Optional[str], env_name: str, label: str) -> str:
    value = arg_value or os.environ.get(env_name)
    if not value:
        sys.stderr.write(f"error: {label} not provided (pass --{label.replace('_', '-').lower()} or set {env_name})\n")
        sys.exit(2)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-wrap device credentials under a new Fernet key.")
    parser.add_argument("--old-key", help="Current Fernet key (defaults to OLD_FERNET_KEY env).")
    parser.add_argument("--new-key", help="New Fernet key (defaults to NEW_FERNET_KEY env).")
    parser.add_argument("--database-url", help="SQLAlchemy URL (defaults to DATABASE_URL env).")
    parser.add_argument("--table", default="devices", help="Table to scan (default: devices).")
    parser.add_argument("--column", default="encrypted_password", help="Column holding the ciphertext (default: encrypted_password).")
    parser.add_argument("--dry-run", action="store_true", help="Decrypt + re-encrypt in memory but don't COMMIT.")
    args = parser.parse_args()

    old_key = _resolve(args.old_key, "OLD_FERNET_KEY", "old_key")
    new_key = _resolve(args.new_key, "NEW_FERNET_KEY", "new_key")
    db_url = _resolve(args.database_url, "DATABASE_URL", "database_url")

    if old_key == new_key:
        sys.stderr.write("error: old key and new key are identical — nothing to do\n")
        return 2

    try:
        old_fernet = Fernet(old_key.encode() if isinstance(old_key, str) else old_key)
        new_fernet = Fernet(new_key.encode() if isinstance(new_key, str) else new_key)
    except Exception as exc:  # cryptography raises a base Exception on malformed keys
        sys.stderr.write(f"error: invalid Fernet key — {exc}\n")
        return 2

    engine = create_engine(db_url)

    processed = 0
    skipped = 0
    errors: list[str] = []

    # Single transaction so a mid-flight crash leaves the table consistent
    # (every row is still readable with the old key).
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"SELECT id, {args.column} FROM {args.table} ORDER BY id")
        ).all()
        print(f"info: scanning {len(rows)} row(s) in {args.table}.{args.column}", file=sys.stderr)

        for row in rows:
            row_id = row[0]
            ciphertext = row[1]

            if ciphertext is None or ciphertext == "":
                skipped += 1
                continue

            try:
                plaintext = old_fernet.decrypt(ciphertext.encode() if isinstance(ciphertext, str) else ciphertext)
            except InvalidToken:
                # Already wrapped under the new key, or wrapped under a key we
                # don't know. Either way: skip and keep going, but record so
                # the operator sees something is off.
                errors.append(f"id={row_id}: decrypt failed (wrong key or already rotated)")
                continue
            except Exception as exc:
                errors.append(f"id={row_id}: decrypt error — {exc}")
                continue

            try:
                new_ciphertext = new_fernet.encrypt(plaintext).decode()
            except Exception as exc:
                errors.append(f"id={row_id}: encrypt error — {exc}")
                continue

            conn.execute(
                text(f"UPDATE {args.table} SET {args.column} = :ct WHERE id = :id"),
                {"ct": new_ciphertext, "id": row_id},
            )
            processed += 1

        if args.dry_run:
            # Roll back so --dry-run leaves no trace.
            conn.rollback()

    summary = (
        f"rewrap-passwords: table={args.table} column={args.column} "
        f"processed={processed} skipped={skipped} errors={len(errors)}"
        f"{' (DRY RUN — rolled back)' if args.dry_run else ''}"
    )
    print(summary)

    if errors:
        sys.stderr.write("errors:\n")
        for line in errors:
            sys.stderr.write(f"  {line}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
