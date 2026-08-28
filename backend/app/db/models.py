from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, unique=True, index=True)
    email = Column(String, nullable=True, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Global system-wide privilege. Fully replaces the pre-MSP ``role``
    # column. Per-scope authorization is expressed through
    # ``role_assignments``.
    is_system_admin = Column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_user_username"),
        UniqueConstraint("email", name="uq_user_email"),
    )


class DeviceModel(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True, index=True)
    host = Column(String, nullable=False)
    vendor = Column(String, nullable=False)
    platform = Column(String, nullable=True)  # nullable for backward compat with existing rows
    username = Column(String, nullable=False)
    encrypted_password = Column(String, nullable=False)
    # Every device belongs to exactly one group; the site is derived through
    # ``device_group.site``.
    device_group_id = Column(
        Integer,
        ForeignKey(
            "device_groups.id", use_alter=True, name="fk_devices_device_group_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    device_group = relationship(
        "DeviceGroupModel",
        foreign_keys=[device_group_id],
        uselist=False,
    )

    __table_args__ = (UniqueConstraint("name", name="uq_device_name"),)


class JobModel(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, nullable=False, unique=True, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    playbook = Column(String, nullable=True)
    device = Column(String, nullable=True)
    parameters = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    max_retries = Column(Integer, nullable=False, default=3)
    rollback_performed = Column(Boolean, nullable=False, default=False)
    rollback_success = Column(Boolean, nullable=True)
    pre_state = Column(JSON, nullable=True)
    last_error = Column(Text, nullable=True)
    current_step = Column(String, nullable=True)
    group_job_id = Column(String, nullable=True, index=True)

    __table_args__ = (
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )


class GroupJobModel(Base):
    __tablename__ = "group_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_job_id = Column(String, nullable=False, unique=True, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    operation = Column(String, nullable=True)
    playbook = Column(String, nullable=True)
    parameters = Column(JSON, nullable=True)
    device_results = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class RefreshTokenModel(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    username = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class LoginAttemptModel(Base):
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, index=True)
    ip_address = Column(String, nullable=False, index=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False, index=True)
    succeeded = Column(Boolean, nullable=False, default=False)


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    user = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    resource = Column(String, nullable=False, index=True)
    resource_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="success")
    details = Column(JSON, nullable=False, default=dict)
    job_id = Column(String, nullable=True)
    device = Column(String, nullable=True)
    request_id = Column(String, nullable=True)
    parent_audit_id = Column(Integer, ForeignKey("audit_logs.id"), nullable=True, index=True)


class DeviceGroupModel(Base):
    __tablename__ = "device_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    site_id = Column(
        Integer,
        ForeignKey("sites.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Marks the Site's Default group. Immutable per D7: cannot be renamed,
    # deleted, or demoted while the Site exists.
    is_default = Column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # foreign_keys disambiguates from the reverse SiteModel.default_group_id
    # FK (two FK paths connect the tables).
    site = relationship(
        "SiteModel",
        back_populates="device_groups",
        foreign_keys=[site_id],
    )

    __table_args__ = (
        UniqueConstraint("site_id", "name", name="uq_device_group_site_name"),
    )


class SiteModel(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True, index=True)
    description = Column(String, nullable=True)
    # MSP: Phase 1 — 'REGULAR' | 'BASE_INFRASTRUCTURE'. Phase 4 adds a partial
    # unique index enforcing exactly one BASE_INFRASTRUCTURE row.
    kind = Column(
        String(32), nullable=False, default="REGULAR", server_default="REGULAR"
    )
    # MSP: Phase 4 (M3) — stays nullable at the DB level despite the plan
    # calling for NOT NULL. The cyclic FK (sites↔device_groups) requires a
    # two-step INSERT (site first with default_group_id=NULL, then default
    # group with its site_id, then back-ref); no portable trick makes that
    # two-step land under a NOT NULL constraint. Enforced instead at the
    # ``site_service.create_site`` layer, which populates the column inside
    # the same transaction. Acknowledged deviation — see CHANGELOG Phase 4.
    default_group_id = Column(
        Integer,
        ForeignKey(
            "device_groups.id", use_alter=True, name="fk_sites_default_group_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    device_groups = relationship(
        "DeviceGroupModel",
        back_populates="site",
        foreign_keys="DeviceGroupModel.site_id",
    )
    # Direct relationship to the Default group. ``post_update=True`` breaks
    # the cyclic FK at flush time.
    default_group = relationship(
        "DeviceGroupModel",
        foreign_keys=[default_group_id],
        post_update=True,
        uselist=False,
    )

    __table_args__ = (UniqueConstraint("name", name="uq_site_name"),)


class RoleAssignmentModel(Base):
    """MSP: Phase 1 — per-scope grant.

    Sole source of per-scope authorization — replaces the pre-MSP global
    user role and site-scoping M2M.
    A user may hold multiple grants; ``effective_role(user, resource)`` picks
    the most specific one at request time (see Phase 3 services/effective_role).

    ``device_group_id IS NULL`` → site-wide grant covering every current and
    future group in the site. ``device_group_id`` set → group-specific grant;
    most-specific wins per resource.
    """

    __tablename__ = "role_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    site_id = Column(
        Integer,
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_group_id = Column(
        Integer,
        ForeignKey("device_groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    role = Column(String(32), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    user = relationship("UserModel", foreign_keys=[user_id])
    created_by = relationship("UserModel", foreign_keys=[created_by_user_id])
    site = relationship("SiteModel")
    device_group = relationship("DeviceGroupModel")

    __table_args__ = (
        CheckConstraint(
            "role IN ('observer', 'operator', 'admin')",
            name="ck_role_assignments_role",
        ),
        UniqueConstraint(
            "user_id",
            "site_id",
            "device_group_id",
            name="uq_role_assignments_scope",
        ),
    )
