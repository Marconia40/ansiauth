"""MSP Phase 2 — post-backfill report.

Emits a Markdown file summarising three side-effects of the M2 backfill
that need operator review:

  1. **Promoted-to-system-admin users** (D24). Every user that ended up with
     ``is_system_admin=TRUE``. The operator reviews the list and demotes any
     entry that shouldn't hold system-wide privilege via Phase 3's
     ``PUT /users/{id}/system-admin`` endpoint.

  2. **Users who lost Base-Infrastructure visibility** (D14). Every
     ``operator`` / ``observer`` who had an empty ``allowed_sites`` set and
     therefore relied on the legacy ``site_id IS NULL`` fallback in
     ``authz.allowed_device_names_for`` to see unassigned devices. Under the
     MSP model those unassigned devices live inside Base Infrastructure,
     which is ``is_system_admin``-only; the fallback no longer applies. The
     operator decides per user whether to grant Base-Infra ``observer`` via
     Phase 3's ``POST /users/{id}/grants``.

  3. **Devices assigned via ambiguous multi-group backfill** (R1). Every
     ``audit_logs`` row with ``action='msp_migration_ambiguous_group_assignment'``
     emitted by Step 4 of the migration. Each row records the pre-migration
     group set the device belonged to inside its site, plus the Default
     group it now points at.

Idempotent: the report file is overwritten on every run. Safe to invoke
against a live database at any time after M2 has been applied.

Usage:
    python -m scripts.msp_post_backfill_report
    # or
    docker compose exec backend python -m scripts.msp_post_backfill_report
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

# Make ``app`` importable when invoked as a plain script from the backend
# working directory (docker compose entrypoint sets PYTHONPATH; a bare
# ``python scripts/...`` invocation needs the fixup).
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import sqlalchemy as sa

from app.db.models import (  # noqa: E402
    AuditLogModel,
    SiteModel,
    UserAllowedSiteModel,
    UserModel,
)
from app.db.session import get_session  # noqa: E402


REPO_ROOT = _BACKEND_DIR.parent
REPORT_DIR = REPO_ROOT / "docs" / "upgrades" / "phases" / "artifacts"


def _promoted_system_admins(session) -> list[dict]:
    rows = (
        session.query(UserModel)
        .filter(UserModel.is_system_admin.is_(True))
        .order_by(UserModel.username)
        .all()
    )
    return [
        {"id": r.id, "username": r.username, "role": r.role}
        for r in rows
    ]


def _lost_base_infra_visibility(session) -> list[dict]:
    """Operator/observer users whose allowed_sites row set is empty.

    These are the users who previously relied on the ``site_id IS NULL``
    fallback in ``authz.allowed_device_names_for`` (see
    ``backend/app/core/authz.py`` §Policy). Under the MSP model that path
    routes to Base Infra, which is ``is_system_admin``-only.
    """
    # Left join so we count users with zero allowed_sites rows.
    subq = (
        session.query(
            UserAllowedSiteModel.user_id,
            sa.func.count(UserAllowedSiteModel.site_id).label("n_sites"),
        )
        .group_by(UserAllowedSiteModel.user_id)
        .subquery()
    )
    rows = (
        session.query(UserModel, sa.func.coalesce(subq.c.n_sites, 0))
        .outerjoin(subq, subq.c.user_id == UserModel.id)
        .filter(UserModel.role.in_(("operator", "observer")))
        .filter(sa.func.coalesce(subq.c.n_sites, 0) == 0)
        .order_by(UserModel.username)
        .all()
    )
    return [
        {"id": u.id, "username": u.username, "role": u.role}
        for u, _n in rows
    ]


def _ambiguous_assignments(session) -> list[dict]:
    rows = (
        session.query(AuditLogModel)
        .filter(
            AuditLogModel.action == "msp_migration_ambiguous_group_assignment"
        )
        .order_by(AuditLogModel.resource_id)
        .all()
    )
    out: list[dict] = []
    for r in rows:
        details = r.details
        # ``details`` is a JSON column; SQLAlchemy decodes on read. If the
        # M2 SQLite branch inserted a hand-built JSON string, coerce.
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = {"raw": details}
        out.append({
            "device": r.resource_id,
            "previous_groups": details.get("previous_groups")
            if isinstance(details, dict) else None,
            "assigned_to": details.get("assigned_to")
            if isinstance(details, dict) else None,
        })
    return out


def _base_infra_site_id(session) -> int | None:
    row = (
        session.query(SiteModel.id)
        .filter(SiteModel.kind == "BASE_INFRASTRUCTURE")
        .first()
    )
    return row[0] if row else None


def _render(section: str, rows: list[dict], columns: list[tuple[str, str]]) -> str:
    """Render one Markdown section — heading + table (or empty-set note)."""
    lines = [f"## {section}\n"]
    if not rows:
        lines.append("_None._\n")
        return "\n".join(lines)
    header = "| " + " | ".join(label for _, label in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    lines.append(header)
    lines.append(sep)
    for row in rows:
        cells = []
        for key, _ in columns:
            v = row.get(key)
            cells.append("" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def generate_report() -> Path:
    with get_session() as session:
        promoted = _promoted_system_admins(session)
        lost_visibility = _lost_base_infra_visibility(session)
        ambiguous = _ambiguous_assignments(session)
        base_infra_id = _base_infra_site_id(session)

    today = date.today().isoformat()
    out_path = REPORT_DIR / f"msp_backfill_report_{today}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    body = [
        f"# MSP Phase 2 — Post-backfill report ({today})\n",
        "Generated by `backend/scripts/msp_post_backfill_report.py`.",
        "See `docs/upgrades/phases/phase-2-backfill.md §4` for context.\n",
        f"- Base Infrastructure site id: `{base_infra_id}`\n",
        _render(
            "1. Promoted-to-system-admin users (D24)",
            promoted,
            [("id", "id"), ("username", "username"), ("role", "prior role")],
        ),
        _render(
            "2. Users who lost Base-Infrastructure visibility (D14)",
            lost_visibility,
            [("id", "id"), ("username", "username"), ("role", "role")],
        ),
        _render(
            "3. Devices assigned via ambiguous multi-group backfill (R1)",
            ambiguous,
            [
                ("device", "device"),
                ("previous_groups", "previous group ids"),
                ("assigned_to", "now in group id"),
            ],
        ),
    ]

    out_path.write_text("\n".join(body), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    path = generate_report()
    print(f"Report written to: {path}")
    # Non-zero exit only if the report couldn't be written; a report with
    # empty tables is a valid outcome.
    if not path.exists():
        sys.exit(1)
    sys.exit(0)
