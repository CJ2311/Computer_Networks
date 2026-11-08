"""Declarative metadata without connection or credential initialization."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
