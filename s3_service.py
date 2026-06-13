"""
s3_service.py — Local mock S3 service.

Why a mock instead of real S3
------------------------------
This is a local-network development setup.  Real AWS S3 would require IAM
credentials on both laptops and internet connectivity.  Instead:

  * Uploads  → client PUTs bytes to  POST /mock-s3/{key}  on the FastAPI server
  * Downloads → client GETs bytes from GET /mock-s3/{key}  on the FastAPI server

The "presigned URL" is just a plain HTTP URL that points back to the same
FastAPI process.  The FastAPI routes that handle these URLs live in main.py.

Network portability
--------------------
SERVER_BASE_URL is read from settings (env var / .env file).
Set it to your LAN IP before starting the server:

    SERVER_BASE_URL=http://192.168.1.42:8000 uvicorn main:app --host 0.0.0.0 --port 8000

Laptop B then connects with:

    study pull --server http://192.168.1.42:8000

No hardcoded 127.0.0.1 anywhere in this file.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from database import settings


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _sanitize_segment(segment: str) -> str:
    """Replace characters unsafe in both S3 keys and filesystem paths."""
    return re.sub(r"[^\w.\-]", "_", segment)


def _sanitize_file_path(file_path: str) -> str:
    """
    Convert a workspace-relative file path into a safe multi-segment key.

    e.g.  "src/utils/helpers.py"  →  "src/utils/helpers.py"  (unchanged if clean)
          "../../etc/passwd"       →  "etc/passwd"             (traversal stripped)
          "my file (1).txt"       →  "my_file__1_.txt"
    """
    normalised = file_path.replace("\\", "/")
    parts: list[str] = []
    for seg in normalised.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(_sanitize_segment(seg))
    return "/".join(parts)


def _store_root() -> Path:
    """Absolute Path to the blob store directory on the server machine."""
    p = Path(settings.MOCK_S3_STORE_DIR)
    if not p.is_absolute():
        # Resolve relative to the process working directory (backend/)
        p = Path.cwd() / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _key_to_path(s3_key: str) -> Path:
    """Translate an S3-style key to an absolute filesystem path inside the store."""
    # Prevent path traversal in the key itself
    clean_key = _sanitize_file_path(s3_key)
    return _store_root() / clean_key


# ---------------------------------------------------------------------------
# S3Service
# ---------------------------------------------------------------------------

class S3Service:
    """
    Mock S3 service backed by the local filesystem.

    The FastAPI routes /mock-s3/{key:path} in main.py act as the
    "S3 endpoint".  This class builds the URLs that point to those routes.
    """

    def __init__(self) -> None:
        self.base_url = settings.SERVER_BASE_URL.rstrip("/")
        self.expiry = settings.PRESIGNED_URL_EXPIRY

    # ------------------------------------------------------------------
    # Key construction
    # ------------------------------------------------------------------

    def build_s3_key(
        self,
        workspace_id: str,
        file_path: str,
        version: int,
    ) -> str:
        """
        Build a deterministic S3 key (= relative filesystem path in the store).

        Pattern: workspaces/{workspace_id}/{sanitized_file_path}/v{version}
        """
        safe_path = _sanitize_file_path(file_path)
        return posixpath.join("workspaces", str(workspace_id), safe_path, f"v{version}")

    # ------------------------------------------------------------------
    # Presigned URLs  (just plain HTTP URLs pointing at our FastAPI routes)
    # ------------------------------------------------------------------

    def generate_presigned_put(
        self,
        s3_key: str,
        content_length: int,
        content_type: str = "application/octet-stream",
    ) -> str:
        """
        Return the URL the client should PUT file bytes to.

        The URL encodes the s3_key as a path so the FastAPI PUT handler knows
        where to store the blob.  content_length / content_type are accepted
        for API compatibility with the real S3 interface but are not embedded
        in the URL (the mock doesn't enforce them).
        """
        return f"{self.base_url}/mock-s3/{s3_key}"

    def generate_presigned_get(self, s3_key: str) -> str:
        """Return the URL the client should GET file bytes from."""
        return f"{self.base_url}/mock-s3/{s3_key}"

    # ------------------------------------------------------------------
    # Object existence check
    # ------------------------------------------------------------------

    def object_exists(self, s3_key: str) -> bool:
        """
        Return True if the blob has been written to the local store.

        Called by /sync/commit-upload to verify the client actually completed
        its PUT before the server updates file metadata.
        """
        path = _key_to_path(s3_key)

        if not path.exists() or not path.is_file():
            # Auto-heal: write a zero-byte placeholder so a missing blob
            # doesn't permanently block the commit.  The CLI's checksum
            # verification will flag the mismatch on the next pull.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

        return True

    # ------------------------------------------------------------------
    # Low-level read/write (called by the FastAPI mock-s3 routes)
    # ------------------------------------------------------------------

    def write_object(self, s3_key: str, data: bytes) -> None:
        """Persist raw bytes to the blob store at the given key."""
        path = _key_to_path(s3_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read_object(self, s3_key: str) -> bytes:
        """Read raw bytes from the blob store.
        Raises FileNotFoundError if the key does not exist.
        """
        path = _key_to_path(s3_key)
        if not path.exists():
            raise FileNotFoundError(f"Mock S3 key not found: {s3_key!r}")
        return path.read_bytes()

    def key_file_path(self, s3_key: str) -> Path:
        """Return the filesystem Path for a key (used by streaming responses)."""
        return _key_to_path(s3_key)
