"""Tests for secure CORS origin configuration."""

import pytest

from backend.app.cors import get_allowed_origins


@pytest.fixture(autouse=True)
def clear_cors_environment(monkeypatch):
    """Ensure every test starts with isolated CORS configuration."""
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)


def test_missing_configuration_denies_all_origins():
    assert get_allowed_origins() == []


def test_empty_configuration_denies_all_origins(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")

    assert get_allowed_origins() == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://[::1]:5173",
        "https://example.com",
        "https://frontend.example.com",
    ],
)
def test_valid_origins(monkeypatch, origin):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origin)

    assert get_allowed_origins() == [origin]


def test_multiple_origins(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )

    assert get_allowed_origins() == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_whitespace_around_entries(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        " http://localhost:5173 , https://example.com ",
    )

    assert get_allowed_origins() == [
        "http://localhost:5173",
        "https://example.com",
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http://*.example.com",
        "localhost:5173",
        "ftp://example.com",
        "file:///tmp/test",
        "http://localhost:5173/path",
        "http://localhost:5173/?test=1",
        "http://localhost:5173/#fragment",
        "http://user:pass@localhost:5173",
        "http://localhost:99999",
        "http://bad_host:5173",
        "http://localhost:5173/",
        "HTTP://localhost:5173",
        "http://LOCALHOST:5173",
        "http://[invalid-ipv6]:5173",
    ],
)
def test_invalid_origins_rejected(monkeypatch, origin):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origin)

    with pytest.raises(ValueError):
        get_allowed_origins()


def test_duplicate_origins_rejected(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5173",
    )

    with pytest.raises(ValueError, match="Duplicate"):
        get_allowed_origins()


@pytest.mark.parametrize(
    "configuration",
    [
        "http://localhost:5173,",
        ",http://localhost:5173",
        "http://localhost:5173,,https://example.com",
    ],
)
def test_empty_entries_rejected(monkeypatch, configuration):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", configuration)

    with pytest.raises(ValueError, match="empty origin"):
        get_allowed_origins()


def test_invalid_origin_rejects_entire_configuration(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,*",
    )

    with pytest.raises(ValueError):
        get_allowed_origins()


def test_cors_configuration_does_not_modify_auth_environment(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173",
    )
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "false")

    assert get_allowed_origins() == ["http://localhost:5173"]

    import os

    assert os.environ["APP_ENV"] == "production"
    assert os.environ["DEV_AUTH_ENABLED"] == "false"