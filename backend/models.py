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


# ---------------------------------------------------------------------------
# SQLite-compatible UUID column type
# ---------------------------------------------------------------------------

class GUID(TypeDecorator):
    """
    Platform-independent UUID type.

    Uses PostgreSQL's native UUID type when available; stores as a 36-char
    VARCHAR string on SQLite (and other databases).  Values are always
    returned as Python ``uuid.UUID`` objects when ``as_uuid=True`` is the
    intent, but here we store/return plain strings to keep things simple
    and avoid any dialect dependency.
    """

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
        return str(value)   # return as string; callers wrap in str() anyway


def _new_uuid() -> str:
    return str(uuid.uuid4())



def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------

class Workspace(Base):
    """
    A named namespace that groups files.  Members authenticate with the
    access_token (UUID v4) rather than passwords.
    """

    __tablename__ = "workspaces"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    name = Column(String(255), nullable=False, unique=True, index=True)
    access_token = Column(
        GUID(),
        nullable=False,
        default=_new_uuid,
        unique=True,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    # relationships
    files = relationship(
        "File",
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    pending_uploads = relationship(
        "PendingUpload",
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    aliases = relationship(
        "Alias",
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )



# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

class File(Base):
    """
    Tracks the *latest* state of a single file path inside a workspace.
    The full history is stored in FileVersion rows.

    latest_version starts at 0 (no content) and increments with every
    successful push.  OCC checks compare the client's base_version against
    this column before issuing a presigned PUT URL.
    """

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("workspace_id", "file_path", name="uq_workspace_file_path"),
    )

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id = Column(
        GUID(),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path = Column(String(2048), nullable=False)
    latest_version = Column(Integer, nullable=False, default=0)
    latest_checksum = Column(String(64), nullable=True)  # SHA-256 hex digest
    size_bytes = Column(BigInteger, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    # relationships
    workspace = relationship("Workspace", back_populates="files")
    versions = relationship(
        "FileVersion",
        back_populates="file",
        cascade="all, delete-orphan",
        order_by="FileVersion.version_number",
        lazy="dynamic",
    )


# ---------------------------------------------------------------------------
# file_versions  (silent history / audit log)
# ---------------------------------------------------------------------------

class FileVersion(Base):
    """
    Immutable append-only record written on every successful commit-upload.
    Enables point-in-time recovery without exposing this complexity to clients.
    """

    __tablename__ = "file_versions"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    file_id = Column(
        GUID(),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)
    checksum = Column(String(64), nullable=False)   # SHA-256 hex digest
    s3_object_key = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    # relationships
    file = relationship("File", back_populates="versions")


# ---------------------------------------------------------------------------
# pending_uploads  (two-phase upload state)
# ---------------------------------------------------------------------------

class PendingUpload(Base):
    """
    Short-lived record that bridges upload-request → commit-upload.

    When the server issues a presigned PUT URL it also writes a PendingUpload
    row.  The client streams the file directly to S3, then calls
    /sync/commit-upload with the upload_id so the server can atomically:
      1. Update files.latest_version / latest_checksum
      2. Insert a FileVersion row
      3. Delete this PendingUpload row

    Rows older than `expires_at` are considered stale and rejected.
    """

    __tablename__ = "pending_uploads"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id = Column(
        GUID(),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path = Column(String(2048), nullable=False)
    checksum = Column(String(64), nullable=False)
    s3_object_key = Column(Text, nullable=False)
    size_bytes = Column(BigInteger, nullable=True)
    new_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    expires_at = Column(DateTime, nullable=False)

    # relationships
    workspace = relationship("Workspace", back_populates="pending_uploads")


# ---------------------------------------------------------------------------
# aliases  (workspace name → UUID mapping)
# ---------------------------------------------------------------------------

class Alias(Base):
    """
    Maps a human-readable alias (workspace name) to a workspace UUID.

    An alias row is written automatically when a workspace is created, using
    the workspace name as the alias.  This lets users join with:

        study join my-project

    instead of a raw UUID token.  Additional aliases can be inserted manually.
    """

    __tablename__ = "aliases"

    id = Column(GUID(), primary_key=True, default=_new_uuid)
    alias_name = Column(String(255), nullable=False, unique=True, index=True)
    workspace_id = Column(
        GUID(),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    # relationships
    workspace = relationship("Workspace", back_populates="aliases")

