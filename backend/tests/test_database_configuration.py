"""Tests for configurable PostgreSQL host, port, and database connection URL."""
import pytest
from sqlalchemy.engine import URL

from backend.app.database import (
    get_database_host,
    get_database_port,
    get_database_url,
)


def test_default_database_host_and_port(monkeypatch):
    """When host and port environment variables are omitted, defaults are localhost and 5432."""
    monkeypatch.delenv("INVENTORY_DB_HOST", raising=False)
    monkeypatch.delenv("INVENTORY_DB_PORT", raising=False)

    assert get_database_host() == "localhost"
    assert get_database_port() == 5432


def test_custom_database_host_and_port(monkeypatch):
    """Custom host and port are correctly parsed and returned."""
    monkeypatch.setenv("INVENTORY_DB_HOST", "postgres.internal.net")
    monkeypatch.setenv("INVENTORY_DB_PORT", "5433")

    assert get_database_host() == "postgres.internal.net"
    assert get_database_port() == 5433


def test_custom_database_url_construction(monkeypatch):
    """URL incorporates custom host, port, database name, and password."""
    monkeypatch.setenv("INVENTORY_DB_HOST", "db-host.cluster.local")
    monkeypatch.setenv("INVENTORY_DB_PORT", "6432")
    monkeypatch.setenv("INVENTORY_APP_PASSWORD", "test-secret-pass")
    monkeypatch.setenv("INVENTORY_DATABASE_NAME", "inventory_production")

    url = get_database_url()
    assert isinstance(url, URL)
    assert url.drivername == "postgresql+psycopg"
    assert url.username == "inventory_app"
    assert url.password == "test-secret-pass"
    assert url.host == "db-host.cluster.local"
    assert url.port == 6432
    assert url.database == "inventory_production"


def test_default_database_name_when_omitted(monkeypatch):
    """Database name defaults to inventory_db when INVENTORY_DATABASE_NAME is unset."""
    monkeypatch.setenv("INVENTORY_APP_PASSWORD", "test-secret-pass")
    monkeypatch.delenv("INVENTORY_DATABASE_NAME", raising=False)

    url = get_database_url()
    assert url.database == "inventory_db"


def test_missing_password_raises_key_error(monkeypatch):
    """Missing INVENTORY_APP_PASSWORD raises KeyError as required by authentication policy."""
    monkeypatch.delenv("INVENTORY_APP_PASSWORD", raising=False)

    with pytest.raises(KeyError):
        get_database_url()


@pytest.mark.parametrize(
    "invalid_port",
    [
        "0",
        "-1",
        "65536",
        "99999",
        "invalid",
        "",
        "   ",
        "5432.5",
        "port5432",
    ],
)
def test_invalid_database_port_rejected(monkeypatch, invalid_port):
    """Port values outside 1-65535 or non-integers raise a descriptive ValueError."""
    monkeypatch.setenv("INVENTORY_DB_PORT", invalid_port)

    with pytest.raises(ValueError, match="Invalid INVENTORY_DB_PORT"):
        get_database_port()


@pytest.mark.parametrize(
    "valid_port, expected",
    [
        ("1", 1),
        ("5432", 5432),
        ("65535", 65535),
        ("  5432  ", 5432),
    ],
)
def test_valid_port_boundary_values(monkeypatch, valid_port, expected):
    """Valid boundary and whitespace-padded port strings parse correctly."""
    monkeypatch.setenv("INVENTORY_DB_PORT", valid_port)
    assert get_database_port() == expected


def test_empty_host_rejected(monkeypatch):
    """Empty or blank INVENTORY_DB_HOST raises ValueError."""
    monkeypatch.setenv("INVENTORY_DB_HOST", "   ")

    with pytest.raises(ValueError, match="INVENTORY_DB_HOST cannot be empty"):
        get_database_host()

