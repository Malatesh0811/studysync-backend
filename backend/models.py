from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base


class GUID(TypeDecorator):
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return str(value)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    memberships = relationship(
        "WorkspaceMember",
        back_populates="user",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------

class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    name = Column(String(255), nullable=False, unique=True, index=True)
    access_token = Column(GUID(), nullable=False, default=_new_uuid, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    files = relationship("File", back_populates="workspace", cascade="all, delete-orphan", lazy="dynamic")
    pending_uploads = relationship("PendingUpload", back_populates="workspace", cascade="all, delete-orphan", lazy="dynamic")
    aliases = relationship("Alias", back_populates="workspace", cascade="all, delete-orphan", lazy="dynamic")
    members = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# workspace_members
# ---------------------------------------------------------------------------

class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_user"),
    )

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id = Column(GUID(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False, default="member")  # "owner" or "member"
    joined_at = Column(DateTime, nullable=False, default=_utcnow)

    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", back_populates="memberships")


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("workspace_id", "file_path", name="uq_workspace_file_path"),
    )

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id = Column(GUID(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    file_path = Column(String(2048), nullable=False)
    latest_version = Column(Integer, nullable=False, default=0)
    latest_checksum = Column(String(64), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    pushed_by = Column(String(255), nullable=True)  # email of last pusher
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    workspace = relationship("Workspace", back_populates="files")
    versions = relationship("FileVersion", back_populates="file", cascade="all, delete-orphan",
                            order_by="FileVersion.version_number", lazy="dynamic")


# ---------------------------------------------------------------------------
# file_versions
# ---------------------------------------------------------------------------

class FileVersion(Base):
    __tablename__ = "file_versions"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    file_id = Column(GUID(), ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    checksum = Column(String(64), nullable=False)
    s3_object_key = Column(Text, nullable=False)
    pushed_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    file = relationship("File", back_populates="versions")


# ---------------------------------------------------------------------------
# pending_uploads
# ---------------------------------------------------------------------------

class PendingUpload(Base):
    __tablename__ = "pending_uploads"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id = Column(GUID(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String(2048), nullable=False)
    checksum = Column(String(64), nullable=False)
    s3_object_key = Column(Text, nullable=False)
    size_bytes = Column(BigInteger, nullable=True)
    new_version = Column(Integer, nullable=False)
    pushed_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    expires_at = Column(DateTime, nullable=False)

    workspace = relationship("Workspace", back_populates="pending_uploads")


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------

class Alias(Base):
    __tablename__ = "aliases"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    alias_name = Column(String(255), nullable=False, unique=True, index=True)
    workspace_id = Column(GUID(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    workspace = relationship("Workspace", back_populates="aliases")
