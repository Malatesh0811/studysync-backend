"""
StudySync FastAPI backend.

Architecture notes
------------------
* Zero-payload: file bytes never pass through this server.  The server only
  manages metadata and issues time-limited S3 presigned URLs.
* Optimistic Concurrency Control (OCC): upload-request compares the client's
  base_version to files.latest_version.  A mismatch returns HTTP 409.
* Silent History: every commit-upload appends a FileVersion row so no data
  is ever truly lost.
* Two-phase upload: upload-request → (client PUT to S3) → commit-upload.
  A PendingUpload row bridges the two phases.

Run locally:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import Alias, File, FileVersion, PendingUpload, Workspace
from s3_service import S3Service

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="StudySync API",
    description="CLI workspace synchronisation platform — metadata & presigned URL broker.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

s3 = S3Service()

PENDING_UPLOAD_TTL_HOURS = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Startup: create tables (use Alembic migrations in production instead)
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _create_tables() -> None:
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Helper — token → Workspace
# ---------------------------------------------------------------------------

def _resolve_workspace(token: str, db: Session) -> Workspace:
    """Resolve a raw token string to a Workspace row or raise 401/400."""
    try:
        token_uuid = uuid.UUID(token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token format — expected a UUID v4 string.",
        )
    workspace = (
        db.query(Workspace).filter(Workspace.access_token == token_uuid).first()
    )
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Workspace not found or token is invalid.",
        )
    return workspace


# ===========================================================================
# /workspaces
# ===========================================================================

class CreateWorkspaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Unique workspace name")


class CreateWorkspaceResponse(BaseModel):
    workspace_id: str
    name: str
    access_token: str
    created_at: str


class JoinWorkspaceRequest(BaseModel):
    token: str = Field(..., description="Workspace access token (UUID)")


class JoinWorkspaceResponse(BaseModel):
    workspace_id: str
    name: str
    message: str


@app.post(
    "/workspaces",
    response_model=CreateWorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new workspace",
    tags=["workspaces"],
)
def create_workspace(
    body: CreateWorkspaceRequest,
    db: Session = Depends(get_db),
) -> CreateWorkspaceResponse:
    """
    Creates a new workspace with a unique name and returns a randomly-generated
    access token that members use to authenticate all subsequent requests.
    """
    existing = db.query(Workspace).filter(Workspace.name == body.name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A workspace named '{body.name}' already exists.",
        )

    workspace = Workspace(name=body.name)
    db.add(workspace)
    db.flush()   # get workspace.id before committing

    # Register the workspace name as its canonical alias
    alias_row = Alias(alias_name=body.name, workspace_id=workspace.id)
    db.add(alias_row)
    db.commit()
    db.refresh(workspace)

    return CreateWorkspaceResponse(
        workspace_id=str(workspace.id),
        name=workspace.name,
        access_token=str(workspace.access_token),
        created_at=workspace.created_at.isoformat(),
    )


@app.post(
    "/workspaces/join",
    response_model=JoinWorkspaceResponse,
    summary="Validate a workspace token",
    tags=["workspaces"],
)
def join_workspace(
    body: JoinWorkspaceRequest,
    db: Session = Depends(get_db),
) -> JoinWorkspaceResponse:
    """
    Validates a token and returns workspace metadata.  The CLI client calls
    this to confirm the token is correct before persisting it locally.
    """
    workspace = _resolve_workspace(body.token, db)
    return JoinWorkspaceResponse(
        workspace_id=str(workspace.id),
        name=workspace.name,
        message="Token is valid.  Welcome to the workspace.",
    )


# ===========================================================================
# /sync/state
# ===========================================================================

class FileStateItem(BaseModel):
    file_path: str
    latest_version: int
    latest_checksum: Optional[str]
    size_bytes: Optional[int]
    updated_at: Optional[str]


class WorkspaceStateResponse(BaseModel):
    workspace_id: str
    name: str
    file_count: int
    files: list[FileStateItem]


@app.get(
    "/sync/state/{workspace_token}",
    response_model=WorkspaceStateResponse,
    summary="Get full workspace file tree",
    tags=["sync"],
)
def get_workspace_state(
    workspace_token: str,
    db: Session = Depends(get_db),
) -> WorkspaceStateResponse:
    """
    Returns the current metadata for every file in the workspace.  The CLI
    client diffs this against its local manifest to decide what to pull.
    """
    workspace = _resolve_workspace(workspace_token, db)
    files: list[File] = db.query(File).filter(
        File.workspace_id == workspace.id
    ).all()

    items = [
        FileStateItem(
            file_path=f.file_path,
            latest_version=f.latest_version,
            latest_checksum=f.latest_checksum,
            size_bytes=f.size_bytes,
            updated_at=f.updated_at.isoformat() if f.updated_at else None,
        )
        for f in files
    ]

    return WorkspaceStateResponse(
        workspace_id=str(workspace.id),
        name=workspace.name,
        file_count=len(items),
        files=items,
    )


# ===========================================================================
# /sync/upload-request
# ===========================================================================

class UploadRequestBody(BaseModel):
    workspace_token: str
    file_path: str = Field(..., min_length=1, max_length=2048)
    base_version: int = Field(..., ge=0, description="Version the client based their edit on")
    checksum: str = Field(..., min_length=64, max_length=64, description="SHA-256 hex digest of the file to upload")
    size_bytes: int = Field(..., gt=0)


class UploadRequestResponse(BaseModel):
    upload_id: str
    presigned_url: str
    s3_object_key: str
    new_version: int
    expires_in_seconds: int


@app.post(
    "/sync/upload-request",
    response_model=UploadRequestResponse,
    summary="OCC check + issue presigned S3 PUT URL",
    tags=["sync"],
)
def request_upload(
    body: UploadRequestBody,
    db: Session = Depends(get_db),
) -> UploadRequestResponse:
    """
    **Optimistic Concurrency Control gate.**

    1. Resolves the workspace from the token.
    2. Looks up the file record (if it exists).
    3. If `base_version` ≠ `latest_version` → **409 Conflict**.
       The client must pull the latest changes before pushing.
    4. Generates the next version number and an S3 object key.
    5. Creates a presigned S3 PUT URL (no file bytes hit this server).
    6. Persists a PendingUpload row so commit-upload can finalise the state.

    The client must call `/sync/commit-upload` after a successful S3 PUT
    to make the new version visible to other clients.
    """
    workspace = _resolve_workspace(body.workspace_token, db)

    # --- OCC check ---
    file_record: Optional[File] = (
        db.query(File)
        .filter(
            File.workspace_id == workspace.id,
            File.file_path == body.file_path,
        )
        .first()
    )

    current_version = file_record.latest_version if file_record else 0

    if body.base_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"OCC conflict on '{body.file_path}': "
                f"remote is at version {current_version} but client base is {body.base_version}. "
                f"Run `study pull` to fetch the latest changes before pushing."
            ),
        )

    # --- Prepare upload slot ---
    new_version = current_version + 1
    s3_key = s3.build_s3_key(str(workspace.id), body.file_path, new_version)

    presigned_url = s3.generate_presigned_put(
        s3_key=s3_key,
        content_length=body.size_bytes,
    )

    pending = PendingUpload(
        workspace_id=workspace.id,
        file_path=body.file_path,
        checksum=body.checksum,
        s3_object_key=s3_key,
        size_bytes=body.size_bytes,
        new_version=new_version,
        expires_at=_utcnow() + timedelta(hours=PENDING_UPLOAD_TTL_HOURS),
    )
    db.add(pending)
    db.commit()
    db.refresh(pending)

    return UploadRequestResponse(
        upload_id=str(pending.id),
        presigned_url=presigned_url,
        s3_object_key=s3_key,
        new_version=new_version,
        expires_in_seconds=PENDING_UPLOAD_TTL_HOURS * 3600,
    )


# ===========================================================================
# /sync/commit-upload
# ===========================================================================

class CommitUploadRequest(BaseModel):
    upload_id: str = Field(..., description="The upload_id returned by /sync/upload-request")


class CommitUploadResponse(BaseModel):
    file_path: str
    new_version: int
    checksum: str
    message: str


@app.post(
    "/sync/commit-upload",
    response_model=CommitUploadResponse,
    summary="Finalise an upload and update workspace state",
    tags=["sync"],
)
def commit_upload(
    body: CommitUploadRequest,
    db: Session = Depends(get_db),
) -> CommitUploadResponse:
    """
    Called by the client *after* a successful S3 PUT.

    Atomically:
    1. Loads and validates the PendingUpload record.
    2. Verifies the S3 object exists (guards against clients lying about
       a successful upload).
    3. Upserts the File row (latest_version, latest_checksum, size_bytes).
    4. Appends a FileVersion row (silent history — no data loss).
    5. Deletes the PendingUpload row.
    """
    try:
        upload_uuid = uuid.UUID(body.upload_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid upload_id — expected a UUID string.",
        )

    pending: Optional[PendingUpload] = (
        db.query(PendingUpload).filter(PendingUpload.id == upload_uuid).first()
    )
    if not pending:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload not found.  It may have already been committed or expired.",
        )

    if pending.expires_at < _utcnow():
        db.delete(pending)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Upload session has expired.  Please re-request an upload slot.",
        )

    # Verify the file was actually written to S3 before updating metadata.
    if not s3.object_exists(pending.s3_object_key):
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail=(
                f"S3 object '{pending.s3_object_key}' not found.  "
                "Complete the PUT to S3 before committing."
            ),
        )

    # --- Upsert File record ---
    file_record: Optional[File] = (
        db.query(File)
        .filter(
            File.workspace_id == pending.workspace_id,
            File.file_path == pending.file_path,
        )
        .first()
    )

    if file_record:
        file_record.latest_version = pending.new_version
        file_record.latest_checksum = pending.checksum
        file_record.size_bytes = pending.size_bytes
        file_record.updated_at = _utcnow()
    else:
        file_record = File(
            workspace_id=pending.workspace_id,
            file_path=pending.file_path,
            latest_version=pending.new_version,
            latest_checksum=pending.checksum,
            size_bytes=pending.size_bytes,
        )
        db.add(file_record)
        db.flush()  # assigns file_record.id before inserting FileVersion

    # --- Append FileVersion (silent history) ---
    version_entry = FileVersion(
        file_id=file_record.id,
        version_number=pending.new_version,
        checksum=pending.checksum,
        s3_object_key=pending.s3_object_key,
    )
    db.add(version_entry)

    # --- Clean up pending slot ---
    db.delete(pending)
    db.commit()

    return CommitUploadResponse(
        file_path=pending.file_path,
        new_version=pending.new_version,
        checksum=pending.checksum,
        message=f"'{pending.file_path}' successfully committed as v{pending.new_version}.",
    )


# ===========================================================================
# /sync/download-request
# ===========================================================================

class DownloadRequestResponse(BaseModel):
    file_path: str
    version: int
    checksum: str
    size_bytes: Optional[int]
    presigned_url: str
    expires_in_seconds: int


@app.get(
    "/sync/download-request",
    response_model=DownloadRequestResponse,
    summary="Issue a presigned S3 GET URL for a file",
    tags=["sync"],
)
def request_download(
    workspace_token: str = Query(..., description="Workspace access token"),
    file_path: str = Query(..., description="Relative file path within the workspace"),
    db: Session = Depends(get_db),
) -> DownloadRequestResponse:
    """
    Returns a time-limited presigned GET URL so the client can stream the
    file directly from S3.  File bytes never pass through this server.
    """
    workspace = _resolve_workspace(workspace_token, db)

    file_record: Optional[File] = (
        db.query(File)
        .filter(
            File.workspace_id == workspace.id,
            File.file_path == file_path,
        )
        .first()
    )
    if not file_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File '{file_path}' does not exist in this workspace.",
        )

    # Look up the latest FileVersion to get its exact S3 key
    latest_ver: Optional[FileVersion] = (
        db.query(FileVersion)
        .filter(
            FileVersion.file_id == file_record.id,
            FileVersion.version_number == file_record.latest_version,
        )
        .first()
    )
    if not latest_ver:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="File metadata is inconsistent — no FileVersion record found.",
        )

    presigned_url = s3.generate_presigned_get(latest_ver.s3_object_key)

    return DownloadRequestResponse(
        file_path=file_path,
        version=file_record.latest_version,
        checksum=file_record.latest_checksum or "",
        size_bytes=file_record.size_bytes,
        presigned_url=presigned_url,
        expires_in_seconds=s3.expiry,
    )


# ---------------------------------------------------------------------------
# Alias / token resolution  — GET /resolve/{input}
# ---------------------------------------------------------------------------


@app.get("/resolve/{input_str}", tags=["workspaces"])
def resolve_workspace(input_str: str, db: Session = Depends(get_db)) -> dict:
    """
    Resolve *input_str* to a workspace token, checking the Aliases table first.

    Algorithm
    ---------
    1. Look up ``input_str`` in the ``aliases`` table.
       If found → return the workspace's token with ``resolved_from_alias=True``.
    2. If not in aliases, try ``input_str`` as a raw UUID access_token.
       If found → return with ``resolved_from_alias=False``.
    3. Otherwise → HTTP 404.

    Response shape
    --------------
    .. code-block:: json

        {
            "resolved_from_alias": true,
            "alias": "my-project",
            "workspace_id": "<uuid>",
            "name": "my-project",
            "access_token": "<uuid>"
        }
    """
    # 1. Alias lookup
    alias_row = db.query(Alias).filter(Alias.alias_name == input_str).first()
    if alias_row:
        ws = db.query(Workspace).filter(Workspace.id == alias_row.workspace_id).first()
        if ws is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Alias points to a deleted workspace.",
            )
        return {
            "resolved_from_alias": True,
            "alias": input_str,
            "workspace_id": str(ws.id),
            "name": ws.name,
            "access_token": str(ws.access_token),
        }

    # 2. Raw UUID (access_token) lookup
    try:
        token_uuid = uuid.UUID(input_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"'{input_str}' is neither a known alias nor a valid UUID token. "
                "Check the value and try again."
            ),
        )

    ws = db.query(Workspace).filter(Workspace.access_token == token_uuid).first()
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace found for token '{input_str}'.",
        )
    return {
        "resolved_from_alias": False,
        "alias": None,
        "workspace_id": str(ws.id),
        "name": ws.name,
        "access_token": str(ws.access_token),
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "studysync-api"}


# ===========================================================================
# Mock S3 routes
#
# The S3Service builds presigned URLs that point at these two endpoints.
# Clients PUT blobs here on upload and GET them back on download.
# No AWS credentials or internet access required.
#
# Key → filesystem mapping is handled entirely by s3.write_object /
# s3.key_file_path so the blob store layout stays in one place (s3_service.py).
# ===========================================================================

from fastapi import Request
from fastapi.responses import FileResponse


@app.put(
    "/mock-s3/{s3_key:path}",
    status_code=200,
    tags=["mock-s3"],
    summary="Receive a file blob (simulates S3 presigned PUT)",
)
async def mock_s3_put(s3_key: str, request: Request) -> dict:
    """
    The CLI streams file bytes here after receiving a presigned PUT URL.
    Bytes are written to the local blob store (MOCK_S3_STORE_DIR / s3_key).
    """
    file_path = s3.key_file_path(s3_key)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with open(file_path, "wb") as fh:
        async for chunk in request.stream():
            fh.write(chunk)

    return {"status": "ok", "key": s3_key}


@app.get(
    "/mock-s3/{s3_key:path}",
    tags=["mock-s3"],
    summary="Serve a file blob (simulates S3 presigned GET)",
)
async def mock_s3_get(s3_key: str) -> FileResponse:
    """
    The CLI fetches file bytes from here during a pull.

    Auto-heal: if the physical blob is missing (e.g. the server was restarted
    and the store directory was wiped), a zero-byte placeholder is written so
    the client can at least complete its pull.  The CLI's checksum verification
    will flag the mismatch; the user should re-push to fix it.
    """
    file_path = s3.key_file_path(s3_key)

    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"")

    return FileResponse(
        path=str(file_path),
        media_type="application/octet-stream",
        filename=file_path.name,
    )
