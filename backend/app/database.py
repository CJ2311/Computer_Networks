import os
from collections.abc import Generator
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from backend.app.base import Base


# Locate the .env file in the project root
BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def get_database_port() -> int:
    """Validate and return the database port from environment."""
    raw = os.getenv("INVENTORY_DB_PORT", "5432")
    try:
        port = int(str(raw).strip())
        if not 1 <= port <= 65535:
            raise ValueError
        return port
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            f"Invalid INVENTORY_DB_PORT: {raw!r}. Must be an integer between 1 and 65535."
        ) from None


def get_database_host() -> str:
    """Return the database host from environment."""
    host = os.getenv("INVENTORY_DB_HOST", "localhost").strip()
    if not host:
        raise ValueError("INVENTORY_DB_HOST cannot be empty.")
    return host


def get_database_url() -> URL:
    """Construct the SQLAlchemy database URL from environment configuration."""
    return URL.create(
        drivername="postgresql+psycopg",
        username="inventory_app",
        password=os.environ["INVENTORY_APP_PASSWORD"],
        host=get_database_host(),
        port=get_database_port(),
        database=os.getenv("INVENTORY_DATABASE_NAME", "inventory_db"),
    )


# Database configuration
DATABASE_URL = get_database_url()


# Create the database engine
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)


# Create database sessions
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


def get_inventory_write_db() -> Generator[Session, None, None]:
    """Fresh write Session; authentication continues using get_db independently."""
    with SessionLocal() as db:
        yield db
