import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker


# Locate the .env file in the project root
BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


# Database configuration
DATABASE_URL = URL.create(
    drivername="postgresql+psycopg",
    username="inventory_app",
    password=os.environ["INVENTORY_APP_PASSWORD"],
    host="localhost",
    port=5432,
    database="inventory_db",
)


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


# Base class for database models
class Base(DeclarativeBase):
    pass

from collections.abc import Generator

from sqlalchemy.orm import Session


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
