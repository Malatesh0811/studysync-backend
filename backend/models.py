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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database import Base


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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, unique=True, index=True)
    access_token = Column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id = Column(
        UUID(as_uuid=True),
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
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
