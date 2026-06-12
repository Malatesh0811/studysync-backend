from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All configuration via environment variables (or a .env file).

    Persistent-volume layout (Docker)
    ----------------------------------
    In production the container expects a volume mounted at /app/data.
    Both SQLite and the blob store live there so a single volume mount
    covers the entire stateful surface of the application:

        docker run ... -v studysync_data:/app/data studysync-backend

    Override any value with an environment variable:

        DATABASE_URL=sqlite:////app/data/studysync.db
        MOCK_S3_STORE_DIR=/app/data/s3_store
        SERVER_BASE_URL=https://your-domain.com
    """

    # -----------------------------------------------------------------------
    # Database
    # -----------------------------------------------------------------------
    # Default points at /app/data so the container works out-of-the-box.
    # For local dev, override with:  DATABASE_URL=sqlite:///./studysync.db
    DATABASE_URL: str = "sqlite:////app/data/studysync.db"

    # -----------------------------------------------------------------------
    # Mock S3 blob store
    # -----------------------------------------------------------------------
    # Absolute path so it is stable regardless of the process working dir.
    MOCK_S3_STORE_DIR: str = "/app/data/s3_store"

    # -----------------------------------------------------------------------
    # Network — CRITICAL for presigned URL generation
    # -----------------------------------------------------------------------
    # Set this to the public URL of your deployed backend so presigned URLs
    # point at a reachable host, e.g.:
    #   SERVER_BASE_URL=https://studysync.onrender.com
    # For LAN testing:
    #   SERVER_BASE_URL=http://192.168.1.42:8000
    SERVER_BASE_URL: str = "http://localhost:8000"

    # How long presigned URLs are advertised as valid (the mock does not
    # enforce expiry, but the CLI respects this for UX display).
    PRESIGNED_URL_EXPIRY: int = 3600

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

# ---------------------------------------------------------------------------
# SQLAlchemy engine
# ---------------------------------------------------------------------------
# check_same_thread=False is required for SQLite when FastAPI's thread pool
# shares a single database file across concurrent requests.
_connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    echo=False,  # flip to True to log every SQL statement
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base — all models inherit from this."""
    pass


def get_db():
    """FastAPI dependency: yields a DB session, rolls back on error."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
