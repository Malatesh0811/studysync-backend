"""
StudySync FastAPI backend.

Architecture notes
------------------
* Zero-payload: file bytes never pass through this server.
* OCC: upload-request compares client base_version to files.latest_version. Mismatch = HTTP 409.
* Silent History: every commit-upload appends a FileVersion row.
* Two-phase upload: upload-request -> (client PUT to S3) -> commit-upload.
* JWT auth: user accounts with workspace ownership and invite system.

Run locally:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import Alias, File, FileVersion, PendingUpload, User, Workspace, WorkspaceMember
from s3_service import S3Service

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="StudySync API",
    description="CLI workspace synchronisation platform with user authentication.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

s3 = S3Service()

PENDING_UPLOAD_TTL_HOURS = 1
SECRET_KEY = os.environ.get("SECRET_KEY", "studysync-dev-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_jwt(user_id: str, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": user_id, "email": email, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")
    return user


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _create_tables() -> None:
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=6, description="Password (min 6 chars)")


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    user_id: str
    email: str
    access_token: str
    token_type: str = "bearer"


@app.post("/auth/register", response_model=AuthResponse, tags=["auth"])
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new user account."""
    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered.")
    user = User(
        email=body.email.lower(),
        password_hash=_hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = _create_jwt(user.id, user.email)
    return AuthResponse(user_id=user.id, email=user.email, access_token=token)


@app.post("/auth/login", response_model=AuthResponse, tags=["auth"])
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Login and receive a JWT access token."""
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not _verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = _create_jwt(user.id, user.email)
    return AuthResponse(user_id=user.id, email=user.email, access_token=token)


# ---------------------------------------------------------------------------
# Workspace endpoints
# ---------------------------------------------------------------------------

class CreateWorkspaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class WorkspaceResponse(BaseModel):
    workspace_id: str
    name: str
    access_token: str
    role: str


@app.post("/workspaces", response_model=WorkspaceResponse, tags=["workspaces"])
def create_workspace(
    body: CreateWorkspaceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_get_current_user),
):
    """Create a new workspace. Caller becomes the owner."""
    existing = db.query(Workspace).filter(Workspace.name == body.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Workspace name already taken.")
    ws = Workspace(name=body.name)
    db.add(ws)
    db.flush()
    # Add creator as owner
    member = WorkspaceMember(workspace_id=ws.id, user_id=current_user.id, role="owner")
    db.add(member)
    # Add alias
    alias = Alias(alias_name=body.name, workspace_id=ws.id)
    db.add(alias)
    db.commit()
    db.refresh(ws)
    return WorkspaceResponse(
        workspace_id=ws.id,
        name=ws.name,
        access_token=ws.access_token,
        role="owner",
    )


class InviteRequest(BaseModel):
    email: str


@app.post("/workspaces/{workspace_id}/invite", tags=["workspaces"])
def invite_member(
    workspace_id: str,
    body: InviteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_get_current_user),
):
    """Invite a registered user to the workspace. Only owner can invite."""
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    # Check caller is owner
    caller_member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == current_user.id,
        WorkspaceMember.role == "owner",
    ).first()
    if not caller_member:
        raise HTTPException(status_code=403, detail="Only the workspace owner can invite members.")
    # Find invitee
    invitee = db.query(User).filter(User.email == body.email.lower()).first()
    if not invitee:
        raise HTTPException(status_code=404, detail="No user found with that email. They must register first.")
    # Check not already a member
    already = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == invitee.id,
    ).first()
    if already:
        raise HTTPException(status_code=400, detail="User is already a member.")
    member = WorkspaceMember(workspace_id=workspace_id, user_id=invitee.id, role="member")
    db.add(member)
    db.commit()
    return {"message": invitee.email + " added to workspace " + ws.name + ".",
            "access_token": ws.access_token, "workspace_name": ws.name}


@app.delete("/workspaces/{workspace_id}/members/{email}", tags=["workspaces"])
def remove_member(
    workspace_id: str,
    email: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(_get_current_user),
):
    """Remove a member from the workspace. Only owner can remove."""
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    caller_member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == current_user.id,
        WorkspaceMember.role == "owner",
    ).first()
    if not caller_member:
        raise HTTPException(status_code=403, detail="Only the workspace owner can remove members.")
    target_user = db.query(User).filter(User.email == email.lower()).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found.")
    if target_user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Owner cannot remove themselves.")
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == target_user.id,
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="User is not a member of this workspace.")
    db.delete(member)
    db.commit()
    return {"message": email + " removed from workspace."}


@app.get("/workspaces/mine", tags=["workspaces"])
def list_my_workspaces(
    db: Session = Depends(get_db),
    current_user: User = Depends(_get_current_user),
):
    """List all workspaces the current user belongs to."""
    memberships = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == current_user.id
    ).all()
    result = []
    for m in memberships:
        ws = db.query(Workspace).filter(Workspace.id == m.workspace_id).first()
        if ws:
            result.append({
                "workspace_id": ws.id,
                "name": ws.name,
                "access_token": ws.access_token,
                "role": m.role,
            })
    return result


@app.post("/workspaces/join", tags=["workspaces"])
def join_workspace(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(_get_current_user),
):
    """Join a workspace using its access token."""
    token = body.get("access_token", "")
    ws = db.query(Workspace).filter(Workspace.access_token == token).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Invalid workspace token.")
    already = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == ws.id,
        WorkspaceMember.user_id == current_user.id,
    ).first()
    if already:
        return {"workspace_id": ws.id, "name": ws.name, "access_token": ws.access_token, "role": already.role}
    member = WorkspaceMember(workspace_id=ws.id, user_id=current_user.id, role="member")
    db.add(member)
    db.commit()
    return {"workspace_id": ws.id, "name": ws.name, "access_token": ws.access_token, "role": "member"}


# ---------------------------------------------------------------------------
# Resolve endpoint
# ---------------------------------------------------------------------------

class ResolveResponse(BaseModel):
    resolved_from_alias: bool
    alias: Optional[str]
    workspace_id: str
    name: str
    access_token: str


@app.get("/resolve/{input}", response_model=ResolveResponse, tags=["workspaces"])
def resolve_workspace(input: str, db: Session = Depends(get_db)):
    alias_row = db.query(Alias).filter(Alias.alias_name == input).first()
    if alias_row:
        ws = db.query(Workspace).filter(Workspace.id == alias_row.workspace_id).first()
        if not ws:
            raise HTTPException(status_code=404, detail="Workspace not found.")
        return ResolveResponse(resolved_from_alias=True, alias=input,
                               workspace_id=ws.id, name=ws.name, access_token=ws.access_token)
    ws = db.query(Workspace).filter(Workspace.access_token == input).first()
    if ws:
        return ResolveResponse(resolved_from_alias=False, alias=None,
                               workspace_id=ws.id, name=ws.name, access_token=ws.access_token)
    raise HTTPException(status_code=404, detail="No workspace found for '" + input + "'.")


# ---------------------------------------------------------------------------
# Sync endpoints
# ---------------------------------------------------------------------------

class SyncStateFile(BaseModel):
    file_path: str
    latest_version: int
    latest_checksum: Optional[str]
    size_bytes: Optional[int]
    pushed_by: Optional[str]


class SyncStateResponse(BaseModel):
    workspace_id: str
    workspace_name: str
    files: list[SyncStateFile]


@app.get("/sync/state/{workspace_token}", response_model=SyncStateResponse, tags=["sync"])
def get_sync_state(workspace_token: str, db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.access_token == workspace_token).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Invalid workspace token.")
    files = db.query(File).filter(File.workspace_id == ws.id).all()
    return SyncStateResponse(
        workspace_id=ws.id,
        workspace_name=ws.name,
        files=[SyncStateFile(
            file_path=f.file_path,
            latest_version=f.latest_version,
            latest_checksum=f.latest_checksum,
            size_bytes=f.size_bytes,
            pushed_by=f.pushed_by,
        ) for f in files],
    )


class UploadRequestBody(BaseModel):
    workspace_token: str
    file_path: str
    checksum: str
    size_bytes: Optional[int] = None
    base_version: int = 0
    pushed_by: Optional[str] = None


class UploadRequestResponse(BaseModel):
    upload_id: str
    presigned_url: str
    new_version: int
    expires_in_seconds: int


@app.post("/sync/upload-request", response_model=UploadRequestResponse, tags=["sync"],
          summary="OCC check + issue presigned S3 PUT URL")
def upload_request(body: UploadRequestBody, db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.access_token == body.workspace_token).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Invalid workspace token.")
    file_row = db.query(File).filter(
        File.workspace_id == ws.id, File.file_path == body.file_path
    ).first()
    current_version = file_row.latest_version if file_row else 0
    if body.base_version != current_version:
        raise HTTPException(status_code=409, detail=(
            "Version conflict: server is at v" + str(current_version)
            + ", client base is v" + str(body.base_version) + ". Pull first."
        ))
    new_version = current_version + 1
    s3_key = s3.make_object_key(str(ws.id), body.file_path, new_version, body.checksum)
    presigned = s3.generate_presigned_put(s3_key, body.size_bytes, "application/octet-stream")
    from models import _new_uuid, _utcnow
    from datetime import timedelta
    pending = PendingUpload(
        workspace_id=ws.id,
        file_path=body.file_path,
        checksum=body.checksum,
        s3_object_key=s3_key,
        size_bytes=body.size_bytes,
        new_version=new_version,
        pushed_by=body.pushed_by,
        expires_at=_utcnow() + timedelta(hours=PENDING_UPLOAD_TTL_HOURS),
    )
    db.add(pending)
    db.commit()
    db.refresh(pending)
    return UploadRequestResponse(
        upload_id=str(pending.id),
        presigned_url=presigned.url,
        new_version=new_version,
        expires_in_seconds=PENDING_UPLOAD_TTL_HOURS * 3600,
    )


class CommitUploadBody(BaseModel):
    upload_id: str


@app.post("/sync/commit-upload", tags=["sync"])
def commit_upload(body: CommitUploadBody, db: Session = Depends(get_db)):
    from models import _utcnow
    pending = db.query(PendingUpload).filter(PendingUpload.id == body.upload_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Upload not found. It may have already been committed or expired.")
    if pending.expires_at < _utcnow():
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=410, detail="Upload session has expired. Please re-request an upload slot.")
    if not s3.object_exists(pending.s3_object_key):
        raise HTTPException(status_code=400, detail=(
            "S3 object '" + pending.s3_object_key + "' not found. Complete the PUT to S3 before committing."
        ))
    file_row = db.query(File).filter(
        File.workspace_id == pending.workspace_id, File.file_path == pending.file_path
    ).first()
    if not file_row:
        file_row = File(workspace_id=pending.workspace_id, file_path=pending.file_path)
        db.add(file_row)
        db.flush()
    file_row.latest_version = pending.new_version
    file_row.latest_checksum = pending.checksum
    file_row.size_bytes = pending.size_bytes
    file_row.pushed_by = pending.pushed_by
    version_row = FileVersion(
        file_id=file_row.id,
        version_number=pending.new_version,
        checksum=pending.checksum,
        s3_object_key=pending.s3_object_key,
        pushed_by=pending.pushed_by,
    )
    db.add(version_row)
    db.delete(pending)
    db.commit()
    return {"message": "Committed.", "version": pending.new_version}


class DownloadResponse(BaseModel):
    presigned_url: str
    file_path: str
    version: int
    expires_in_seconds: int


@app.get("/sync/download-request", response_model=DownloadResponse, tags=["sync"])
def download_request(
    workspace_token: str = Query(...),
    file_path: str = Query(...),
    db: Session = Depends(get_db),
):
    ws = db.query(Workspace).filter(Workspace.access_token == workspace_token).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Invalid workspace token.")
    file_row = db.query(File).filter(
        File.workspace_id == ws.id, File.file_path == file_path
    ).first()
    if not file_row:
        raise HTTPException(status_code=404, detail="File not found in workspace.")
    latest_ver = db.query(FileVersion).filter(
        FileVersion.file_id == file_row.id,
        FileVersion.version_number == file_row.latest_version,
    ).first()
    if not latest_ver:
        raise HTTPException(status_code=404, detail="No committed version found.")
    presigned = s3.generate_presigned_get(latest_ver.s3_object_key)
    return DownloadResponse(
        presigned_url=presigned.url,
        file_path=file_path,
        version=file_row.latest_version,
        expires_in_seconds=presigned.expiry,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Mock S3 routes
# ---------------------------------------------------------------------------

from fastapi import Request
from fastapi.responses import Response


@app.put("/mock-s3/{s3_key:path}", tags=["mock-s3"], include_in_schema=False)
async def mock_s3_put(s3_key: str, request: Request):
    body = await request.body()
    s3.write_object(s3_key, body)
    return Response(status_code=200)


@app.get("/mock-s3/{s3_key:path}", tags=["mock-s3"], include_in_schema=False)
def mock_s3_get(s3_key: str):
    try:
        data = s3.read_object(s3_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Object not found.")
    return Response(content=data, media_type="application/octet-stream")
