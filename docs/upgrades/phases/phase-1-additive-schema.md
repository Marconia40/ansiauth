# Phase 1 — Additive Schema (M1)

> **Goal.** Land every new column/table needed by MSP as **nullable, defaulted, additive**. No behavioral change; no service or API path reads or writes the new columns yet. Existing tests continue to pass unchanged.
> **Preconditions:** Phase 0 exit criteria met.
> **Design principles:** see [phase-0-prerequisites.md §1](phase-0-prerequisites.md#1-cross-phase-design-principles-apply-to-every-phase).
> **Companion plan sections:** §11, §20.2, §23 (M1), §32 T1.1–T1.3.

---

## 1. Scope summary

| Layer | Change | Behavior change? |
|---|---|---|
| Alembic | 1 new revision `msp_additive` | No |
| ORM (`backend/app/db/models.py`) | Add columns + relationships, all nullable/defaulted | No |
| App startup (`backend/app/main.py`) | Idempotent Base-Infra seed (writes to the new columns only) | Yes: adds 1 row on first boot |
| Services | **Only** `site_service.ensure_base_infrastructure()` (new) | Only invoked from startup |
| API | **Untouched** | — |
| Frontend | **Untouched** | — |

Total new files: **1** (Alembic revision).
Total modified files: **3** (`db/models.py`, `main.py`, `services/site_service.py`).
Total new functions: **1** (`site_service.ensure_base_infrastructure`).

---

## 2. Alembic migration — T1.1

### File
`backend/migrations/versions/<yyyyMMddHHmm>_msp_additive.py`

### Revision metadata
```python
revision = "e1msp1_additive"
down_revision = "d8a5f2c1b630"
branch_labels = None
depends_on = None
```

### `upgrade()` — every statement additive, every column nullable, no drops

```python
def upgrade() -> None:
    # 1. sites.kind — REGULAR default; only Base Infra will hold BASE_INFRASTRUCTURE
    op.add_column(
        "sites",
        sa.Column("kind", sa.String(length=32), nullable=False,
                  server_default="REGULAR"),
    )

    # 2. sites.default_group_id — nullable at DB layer to solve the cyclic FK
    #    (see MSP_IMPLEMENTATION_PLAN.md §11.6 Option A).
    #    App enforces NOT NULL post-M2.
    op.add_column(
        "sites",
        sa.Column("default_group_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_sites_default_group_id_device_groups",
        source_table="sites", referent_table="device_groups",
        local_cols=["default_group_id"], remote_cols=["id"],
        ondelete="RESTRICT",
        use_alter=True,   # cycle-safe
    )

    # 3. device_groups.is_default — False default; partial unique index added in M3
    op.add_column(
        "device_groups",
        sa.Column("is_default", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )

    # 4. devices.device_group_id — nullable; populated in M2; NOT NULL in M3
    op.add_column(
        "devices",
        sa.Column("device_group_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_devices_device_group_id_device_groups",
        source_table="devices", referent_table="device_groups",
        local_cols=["device_group_id"], remote_cols=["id"],
        ondelete="RESTRICT",
        use_alter=True,
    )
    op.create_index(
        "ix_devices_device_group_id",
        "devices", ["device_group_id"],
    )

    # 5. users.is_system_admin — False default; populated in M2 Step 6a
    op.add_column(
        "users",
        sa.Column("is_system_admin", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )

    # 6. role_assignments — new table, populated in M2 Step 6b
    op.create_table(
        "role_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("device_group_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_group_id"], ["device_groups.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"],
                                ondelete="SET NULL"),
        sa.CheckConstraint(
            "role IN ('observer', 'operator', 'admin')",
            name="ck_role_assignments_role",
        ),
        sa.UniqueConstraint("user_id", "site_id", "device_group_id",
                            name="uq_role_assignments_scope"),
    )
    op.create_index("ix_role_assignments_user", "role_assignments", ["user_id"])
    op.create_index("ix_role_assignments_site", "role_assignments", ["site_id"])
    op.create_index(
        "ix_role_assignments_group", "role_assignments", ["device_group_id"],
        postgresql_where=sa.text("device_group_id IS NOT NULL"),
        sqlite_where=sa.text("device_group_id IS NOT NULL"),
    )
```

### `downgrade()`
Reverse each of the six blocks in reverse order. Drop the `use_alter=True` FKs by name before their columns.

### Test — T1.1 test
`backend/tests/migrations/test_msp_m1_roundtrip.py`
- Seed DB with the existing `d8a5f2c1b630` schema + 1 site, 1 group, 2 devices, 2 users.
- Run `alembic upgrade head` (lands M1) → assert every new column exists via `information_schema.columns`.
- Run `alembic downgrade -1` → assert every new column removed.

---

## 3. ORM extensions — T1.2

### File
`backend/app/db/models.py`

### Additions to `SiteModel` (currently lines 201–228)
```python
kind = Column(String(32), nullable=False, server_default="REGULAR")
default_group_id = Column(
    Integer,
    ForeignKey("device_groups.id", use_alter=True, ondelete="RESTRICT"),
    nullable=True,
)
default_group = relationship(
    "DeviceGroupModel",
    foreign_keys=[default_group_id],
    post_update=True,        # required — post_update breaks the cycle at flush time
    uselist=False,
)
```

### Additions to `DeviceGroupModel` (currently lines 173–200)
```python
is_default = Column(Boolean, nullable=False, server_default=sa.false())
# Existing `site_id` FK remains nullable for now — flipped to NOT NULL in M3 (Phase 4).
```

### Additions to `DeviceModel` (currently lines 60–91)
```python
device_group_id = Column(
    Integer,
    ForeignKey("device_groups.id", use_alter=True, ondelete="RESTRICT"),
    nullable=True,
    index=True,
)
device_group = relationship(
    "DeviceGroupModel",
    foreign_keys=[device_group_id],
    uselist=False,
)
# `site_id` remains — dropped in M4 (Phase 5).
# `group_members` (M2M) remains — dropped in M4 (Phase 5).
```

### Additions to `UserModel` (currently lines 9–41)
```python
is_system_admin = Column(Boolean, nullable=False, server_default=sa.false())
# `role` column remains — dropped in M4 (Phase 5).
```

### New model class `RoleAssignmentModel`
Add **once**, placed adjacent to `UserAllowedSiteModel`:
```python
class RoleAssignmentModel(Base):
    __tablename__ = "role_assignments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    device_group_id = Column(Integer,
                             ForeignKey("device_groups.id", ondelete="CASCADE"),
                             nullable=True)
    role = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by_user_id = Column(Integer,
                                ForeignKey("users.id", ondelete="SET NULL"),
                                nullable=True)

    __table_args__ = (
        CheckConstraint(
            "role IN ('observer', 'operator', 'admin')",
            name="ck_role_assignments_role",
        ),
        UniqueConstraint("user_id", "site_id", "device_group_id",
                         name="uq_role_assignments_scope"),
    )
```

### Test — T1.2 test
`backend/tests/test_msp_m1_models_expose_columns.py`
- Import each model, assert `hasattr(SiteModel, "kind")`, `hasattr(SiteModel, "default_group_id")`, etc.
- Assert `RoleAssignmentModel.__tablename__ == "role_assignments"`.
- No behavioral assertions — this is a smoke test for the additions.

---

## 4. Base-Infrastructure bootstrap — T1.3

### 4.1 New service function
`backend/app/services/site_service.py`, appended at the bottom of the file:

```python
BASE_INFRA_NAME = "Base Infrastructure"
BASE_INFRA_KIND = "BASE_INFRASTRUCTURE"
DEFAULT_GROUP_NAME = "Default"

def ensure_base_infrastructure() -> int:
    """MSP: Phase 1 — idempotent bootstrap.
    Returns Base Infrastructure site id."""
    with get_session() as db:
        site = db.query(SiteModel).filter(SiteModel.kind == BASE_INFRA_KIND).first()
        if site is None:
            site = SiteModel(name=BASE_INFRA_NAME, kind=BASE_INFRA_KIND,
                             description="System-managed base infrastructure site.")
            db.add(site)
            db.flush()

        if site.default_group_id is None:
            group = DeviceGroupModel(name=DEFAULT_GROUP_NAME, site_id=site.id,
                                     is_default=True,
                                     description="Default group for Base Infrastructure.")
            db.add(group)
            db.flush()
            site.default_group_id = group.id

        db.commit()
        return site.id
```

**Traceability:** the *only* function that writes MSP-additive columns in Phase 1. Grep-locatable via `# MSP: Phase 1` comment on line 1.

### 4.2 Wire into startup
`backend/app/main.py` — inside the existing FastAPI startup hook (already seeds `admin` user; extend, do not add a new hook):

```python
from app.services.site_service import ensure_base_infrastructure

@app.on_event("startup")   # or `lifespan` equivalent — extend the existing handler
def _startup():
    # ... existing seed_defaults() call ...
    ensure_base_infrastructure()   # MSP: Phase 1
```

### 4.3 Test — T1.3 test
`backend/tests/test_msp_m1_base_infra_bootstrap.py`
- Start app in TestClient twice.
- Assert `SELECT COUNT(*) FROM sites WHERE kind='BASE_INFRASTRUCTURE'` == 1.
- Assert Base Infra site has non-null `default_group_id`.
- Assert group referenced by `default_group_id` has `is_default=True` and `name='Default'`.

---

## 5. Request-flow after Phase 1

**Unchanged** from today, with one exception:

```
FastAPI startup
   ↓
ensure_base_infrastructure()   ← writes 1 site + 1 group, idempotent
   ↓
existing seed_defaults()       ← unchanged
```

Every regular API request path is byte-identical to pre-Phase-1. No endpoint, no dependency, no service function reads or writes the new columns during a request in this phase.

---

## 6. Merge strategy

- **Mergeable to `main`?** ✅ **Yes, as a standalone PR.**
- **Rationale:** all additions are nullable/defaulted; the only new behavior is a boot-time seed that produces a Site invisible to non-super-admins (Base Infra) and is idempotent.
- **PR checklist:**
  - [ ] `backend/migrations/versions/<ts>_msp_additive.py`
  - [ ] `backend/app/db/models.py` — additions only, no removals
  - [ ] `backend/app/services/site_service.py` — `ensure_base_infrastructure` appended
  - [ ] `backend/app/main.py` — one-line addition to startup
  - [ ] `backend/tests/migrations/test_msp_m1_roundtrip.py`
  - [ ] `backend/tests/test_msp_m1_models_expose_columns.py`
  - [ ] `backend/tests/test_msp_m1_base_infra_bootstrap.py`
  - [ ] `CHANGELOG.md` — appended `### Added` bullets
- **Release notes:** none needed for API clients; devs may notice a new "Base Infrastructure" row in `SELECT * FROM sites`.

---

## 7. Rollback

`alembic downgrade -1` reverses M1 in full. Drop-order handles the cyclic FK because `default_group_id`'s FK is `use_alter=True` and is dropped by name before the `default_group_id` column.

The `ensure_base_infrastructure()` call remains harmless on downgraded schemas because the `kind`/`default_group_id` columns will not exist and the ORM will surface a `NoSuchColumn` error at import — so the call must be guarded by feature-flag *or* removed together with the ORM additions on rollback. Practically: rollback = revert this entire PR.

---

## 8. Exit criteria

- [ ] `alembic upgrade head` runs clean against a snapshot of production data.
- [ ] `alembic downgrade -1 && alembic upgrade head` cycles cleanly.
- [ ] All existing `backend/tests/` still pass.
- [ ] Three new Phase-1 tests pass.
- [ ] `SELECT COUNT(*) FROM sites WHERE kind='BASE_INFRASTRUCTURE'` returns 1 after first boot.
- [ ] `CHANGELOG.md` updated.
