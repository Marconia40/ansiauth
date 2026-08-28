"""MSP: Phase 4 T4.1 — M3 enforces every invariant at the DB layer.

Covers the partial unique indexes and per-site UNIQUE(site_id, name) added
by ``e4msp3_msp_enforce``. NOT NULL flips are exercised implicitly by every
other test in the suite (every ``devices`` row inserted post-Phase-4 has a
``device_group_id``); the checks here focus on the invariants that no
prior test suite covered.

Note: These tests do NOT re-invoke the Alembic migration — the pytest
session already ran ``alembic upgrade head`` in ``conftest.py``. They
verify that the constraints are actually installed on the live schema and
that violating them raises ``IntegrityError``.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    SiteModel,
)
from app.db.session import get_session


BASE_INFRA_KIND = "BASE_INFRASTRUCTURE"


def _cleanup_site(site_id: int) -> None:
    """Best-effort teardown: drop devices in the site's groups, drop grants,
    null the site's default_group_id, drop groups, then the site. Skipped
    rows that are already gone are fine."""
    from app.db.models import RoleAssignmentModel
    with get_session() as session:
        group_ids = [
            r[0]
            for r in session.query(DeviceGroupModel.id).filter_by(site_id=site_id).all()
        ]
        if group_ids:
            session.query(DeviceModel).filter(
                DeviceModel.device_group_id.in_(group_ids)
            ).delete(synchronize_session=False)
        session.query(RoleAssignmentModel).filter_by(site_id=site_id).delete(synchronize_session=False)
        row = session.query(SiteModel).filter_by(id=site_id).first()
        if row is not None:
            row.default_group_id = None
        session.flush()
        session.query(DeviceGroupModel).filter_by(site_id=site_id).delete(synchronize_session=False)
        session.query(SiteModel).filter_by(id=site_id).delete(synchronize_session=False)


# ── ux_sites_single_base_infra ───────────────────────────────────────────────

def test_second_base_infrastructure_site_rejected():
    """The partial unique index ``ux_sites_single_base_infra`` guarantees at
    most one row with ``kind='BASE_INFRASTRUCTURE'``. Attempting to insert a
    second one must raise IntegrityError."""
    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.add(SiteModel(
                name=f"impostor-base-{uuid.uuid4().hex[:6]}",
                kind=BASE_INFRA_KIND,
            ))


# ── ux_device_groups_one_default_per_site ────────────────────────────────────

def test_second_default_group_in_same_site_rejected():
    """One is_default=TRUE per site (D7). Attempting to add a second default
    inside the same site must raise IntegrityError."""
    site_name = f"m3-dup-def-{uuid.uuid4().hex[:6]}"
    site_id = None
    try:
        with get_session() as session:
            site = SiteModel(name=site_name, kind="REGULAR")
            session.add(site)
            session.flush()
            site_id = site.id
            g1 = DeviceGroupModel(name="Default", site_id=site.id, is_default=True)
            session.add(g1)
            session.flush()
            site.default_group_id = g1.id

        with pytest.raises(IntegrityError):
            with get_session() as session:
                session.add(DeviceGroupModel(
                    name="ExtraDefault", site_id=site_id, is_default=True,
                ))
    finally:
        if site_id is not None:
            _cleanup_site(site_id)


def test_two_default_groups_in_different_sites_allowed():
    """The partial unique is scoped by ``site_id``; every site is allowed
    exactly one is_default group, so two sites can each have their own."""
    tag = uuid.uuid4().hex[:6]
    a_name = f"m3-def-A-{tag}"
    b_name = f"m3-def-B-{tag}"
    a_id = b_id = None
    try:
        for name in (a_name, b_name):
            with get_session() as session:
                site = SiteModel(name=name, kind="REGULAR")
                session.add(site)
                session.flush()
                g = DeviceGroupModel(name="Default", site_id=site.id, is_default=True)
                session.add(g)
                session.flush()
                site.default_group_id = g.id
                if name == a_name:
                    a_id = site.id
                else:
                    b_id = site.id
    finally:
        for sid in (b_id, a_id):
            if sid is not None:
                _cleanup_site(sid)


# ── uq_device_group_site_name (D6) ───────────────────────────────────────────

def test_two_groups_same_name_same_site_rejected():
    """UNIQUE(site_id, name) blocks two groups with the same name in the
    same site (D6)."""
    site_name = f"m3-uq-dup-{uuid.uuid4().hex[:6]}"
    site_id = None
    try:
        with get_session() as session:
            site = SiteModel(name=site_name, kind="REGULAR")
            session.add(site)
            session.flush()
            site_id = site.id
            g1 = DeviceGroupModel(name="engineering", site_id=site.id, is_default=False)
            session.add(g1)
            session.flush()

        with pytest.raises(IntegrityError):
            with get_session() as session:
                session.add(DeviceGroupModel(
                    name="engineering", site_id=site_id, is_default=False,
                ))
    finally:
        if site_id is not None:
            _cleanup_site(site_id)


def test_two_groups_same_name_different_sites_allowed():
    """Per-site UNIQUE(site_id, name) permits the same name across sites —
    every site can have its own "engineering" or "Default" group."""
    tag = uuid.uuid4().hex[:6]
    site_ids = []
    try:
        for name in (f"m3-uq-A-{tag}", f"m3-uq-B-{tag}"):
            with get_session() as session:
                site = SiteModel(name=name, kind="REGULAR")
                session.add(site)
                session.flush()
                site_ids.append(site.id)
                session.add(DeviceGroupModel(
                    name="engineering", site_id=site.id, is_default=False,
                ))
    finally:
        for sid in reversed(site_ids):
            _cleanup_site(sid)


# ── FK RESTRICT on device_groups.site_id ─────────────────────────────────────

def test_delete_site_with_groups_blocked_by_restrict_fk():
    """M3 flips ``device_groups.site_id → sites.id`` to RESTRICT. A raw
    DELETE of the site while a group still points at it must raise
    IntegrityError (SQLite now enforces FKs via PRAGMA foreign_keys=ON)."""
    site_name = f"m3-fk-{uuid.uuid4().hex[:6]}"
    site_id = None
    try:
        with get_session() as session:
            site = SiteModel(name=site_name, kind="REGULAR")
            session.add(site)
            session.flush()
            site_id = site.id
            session.add(DeviceGroupModel(
                name="Default", site_id=site.id, is_default=True,
            ))

        with pytest.raises(IntegrityError):
            with get_session() as session:
                session.query(SiteModel).filter_by(id=site_id).delete(
                    synchronize_session=False,
                )
    finally:
        if site_id is not None:
            _cleanup_site(site_id)
