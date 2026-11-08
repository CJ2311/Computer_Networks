"""Unit tests must never use the application's host database engine."""
import pytest


@pytest.fixture(autouse=True)
def forbid_application_database(monkeypatch):
    from backend.app.database import engine
    def forbidden(*args, **kwargs):
        raise AssertionError("Host application database access is forbidden in tests")
    monkeypatch.setattr(engine, "connect", forbidden)
    monkeypatch.setattr(engine, "raw_connection", forbidden)
