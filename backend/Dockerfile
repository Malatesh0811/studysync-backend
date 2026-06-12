# =============================================================================
# StudySync Backend — Production Dockerfile
#
# Multi-stage build:
#   Stage 1 (builder)  — installs Python dependencies into an isolated prefix
#   Stage 2 (runtime)  — copies only the installed packages + app code,
#                        resulting in a lean final image with no build tools
#
# Persistent data volume
# ----------------------
# All stateful data lives under /app/data:
#   /app/data/studysync.db   — SQLite database
#   /app/data/s3_store/      — blob store (mock S3)
#
# Mount a named volume or cloud disk here to survive container restarts:
#   docker run -v studysync_data:/app/data ...
#
# On Render:  add a Disk with Mount Path = /app/data
# On AWS ECS: add an EBS/EFS volume mounted at /app/data
# =============================================================================

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

# Upgrade pip once in the builder so the runtime image stays clean.
RUN pip install --upgrade pip --no-cache-dir

# Copy the requirements file first so Docker's layer cache skips reinstalling
# packages when only application code changes.
COPY requirements.txt .

# Install into an isolated prefix (/install) that we copy wholesale into
# the runtime stage — this keeps build tools out of the final image.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

# curl is the only runtime system package needed for the HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app

# ── Installed packages (from builder) ─────────────────────────────────────────
COPY --from=builder /install /usr/local

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# ── Persistent data directory ─────────────────────────────────────────────────
# /app/data is the single mount-point for all stateful data.
# Creating it here (owned by appuser) means the app can write to it even when
# no external volume is mounted — useful for quick smoke-tests.
RUN mkdir -p /app/data/s3_store \
    && chown -R appuser:appgroup /app/data

# Declare the mount point so runtimes and orchestrators know this path is
# expected to be backed by persistent storage.
VOLUME ["/app/data"]

# ── Switch to non-root ────────────────────────────────────────────────────────
USER appuser

# ── Environment defaults ──────────────────────────────────────────────────────
# Override SERVER_BASE_URL at deploy time with your public hostname, e.g.:
#   SERVER_BASE_URL=https://studysync.onrender.com
#
# PYTHONDONTWRITEBYTECODE prevents .pyc files from polluting the image layers.
# PYTHONUNBUFFERED ensures logs appear in real-time in cloud log streams.
ENV DATABASE_URL="sqlite:////app/data/studysync.db" \
    MOCK_S3_STORE_DIR="/app/data/s3_store" \
    SERVER_BASE_URL="http://localhost:8000" \
    PRESIGNED_URL_EXPIRY="3600" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

# ── Health check ──────────────────────────────────────────────────────────────
# Cloud providers (Render, ECS, Fly.io) use this to gate traffic routing.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# ── Start command ─────────────────────────────────────────────────────────────
# --workers 1  : SQLite does not support concurrent writers; keep at 1 unless
#                you swap to PostgreSQL.
# --host 0.0.0.0: required for Docker port-mapping to work.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--access-log"]
