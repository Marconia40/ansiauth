from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.base import Base


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, unique=True, index=True)
    email = Column(String, nullable=True, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="observer")
    is_active = Column(Boolean, nullable=False, default=True)
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

    allowed_sites = relationship(
        "SiteModel",
        secondary="user_allowed_sites",
        back_populates="allowed_users",
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_user_username"),
        UniqueConstraint("email", name="uq_user_email"),
    )


class UserAllowedSiteModel(Base):
    """Many-to-many association table: which sites a (non-admin) user may access."""

    __tablename__ = "user_allowed_sites"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    site_id = Column(
        Integer, ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
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
    site_id = Column(
        Integer,
        ForeignKey("sites.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    group_members = relationship(
        "DeviceGroupMemberModel",
        back_populates="device",
        cascade="all, delete-orphan",
    )
    site = relationship("SiteModel", back_populates="devices")

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
    name = Column(String, nullable=False, unique=True, index=True)
    description = Column(String, nullable=True)
    site_id = Column(
        Integer,
        ForeignKey("sites.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    members = relationship(
        "DeviceGroupMemberModel",
        back_populates="group",
        cascade="all, delete-orphan",
    )
    site = relationship("SiteModel", back_populates="device_groups")

    __table_args__ = (UniqueConstraint("name", name="uq_device_group_name"),)


class SiteModel(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True, index=True)
    description = Column(String, nullable=True)
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

    devices = relationship("DeviceModel", back_populates="site")
    device_groups = relationship("DeviceGroupModel", back_populates="site")
    allowed_users = relationship(
        "UserModel",
        secondary="user_allowed_sites",
        back_populates="allowed_sites",
    )

    __table_args__ = (UniqueConstraint("name", name="uq_site_name"),)


class DeviceGroupMemberModel(Base):
    __tablename__ = "device_group_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("device_groups.id"), nullable=False, index=True)
    device_name = Column(String, ForeignKey("devices.name"), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    group = relationship("DeviceGroupModel", back_populates="members")
    device = relationship("DeviceModel", back_populates="group_members")

    __table_args__ = (
        UniqueConstraint("group_id", "device_name", name="uq_group_member"),
    )
