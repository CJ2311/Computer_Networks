"""No network/database: exercise admission, provider failures and HTTP contracts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import Mock

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from backend.app.auth import college
from backend.app.auth.dependencies import get_current_user
from backend.app.auth.introspection_limits import IntrospectionGuard, IntrospectionSettings
from backend.app.database import get_db
from backend.app.main import app
from backend.app.models.user import User
from backend.tests.test_college_auth import provider
from backend.tests.test_request_limits import request


@pytest.fixture
def protected(provider, monkeypatch):
    now = [100.0]
    guard = IntrospectionGuard(IntrospectionSettings(max_concurrent=1, global_per_minute=10,
                                                    peer_per_minute=2, max_peers=2), lambda: now[0])
    monkeypatch.setattr(college, "get_introspection_guard", lambda: guard)
    return guard, now, provider


def status(call, expected):
    with pytest.raises(HTTPException) as error:
        call()
    assert error.value.status_code == expected
    assert "synthetic" not in error.value.detail
    return error.value


@pytest.mark.parametrize("authorization", [None, "", "Basic value", "Bearer", "Bearer bad token", "Bearer " + "x" * 8193])
def test_malformed_tokens_count_and_never_reach_provider(protected, authorization):
    _, _, provider = protected
    for _ in range(2):
        status(lambda: college.introspect(authorization), 401)
    error = status(lambda: college.introspect("Bearer newly-rotated-token"), 429)
    assert int(error.headers["Retry-After"]) >= 1
    provider[2].open.assert_not_called()


@pytest.mark.parametrize("invalid", [False, True])
def test_valid_and_invalid_tokens_share_budget_and_recover(protected, invalid):
    guard, now, provider = protected
    provider[1]["active"] = not invalid
    for token in ("first", "second"):
        if invalid:
            status(lambda: college.introspect("Bearer " + token, client_host="198.51.100.1"), 401)
        else:
            assert college.introspect("Bearer " + token, client_host="198.51.100.1")[1] == "subject-1"
    status(lambda: college.introspect("Bearer third", client_host="198.51.100.1"), 429)
    assert provider[2].open.call_count == 2
    provider[1]["active"] = True
    now[0] += 30
    assert college.introspect("Bearer recovered", client_host="198.51.100.1")[1] == "subject-1"
    assert len(guard.peers) == 1


def test_global_limit_cannot_be_bypassed_by_rotating_peers():
    guard = IntrospectionGuard(IntrospectionSettings(global_per_minute=2, peer_per_minute=10))
    guard.admit("198.51.100.1")
    guard.admit("198.51.100.2")
    status(lambda: guard.admit("198.51.100.3"), 429)
    assert len(guard.peers) == 2


def test_peer_tracking_is_bounded_without_eviction_bypass(protected):
    guard, now, _ = protected
    guard.admit("198.51.100.1")
    guard.admit("198.51.100.2")
    status(lambda: guard.admit("198.51.100.3"), 429)
    assert len(guard.peers) == 2
    guard.admit("198.51.100.1")
    status(lambda: guard.admit("198.51.100.1"), 429)
    now[0] += 60
    guard.admit("198.51.100.3")
    assert list(guard.peers) == ["198.51.100.3"]


@pytest.mark.parametrize("hosts", [[None, "not-an-ip", "another-name"],
                                    ["2001:db8::1", "2001:0db8:0:0:0:0:0:1", "2001:db8::1"]])
def test_unknown_and_equivalent_peer_forms_cannot_rotate_identity(protected, hosts):
    guard, _, _ = protected
    guard.admit(hosts[0])
    guard.admit(hosts[1])
    status(lambda: guard.admit(hosts[2]), 429)
    assert len(guard.peers) == 1


def test_simultaneous_admission_is_atomic():
    guard = IntrospectionGuard(IntrospectionSettings(global_per_minute=2, peer_per_minute=2))
    def attempt(_):
        try:
            guard.admit("198.51.100.1")
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(20)))
    assert results.count(200) == 2 and results.count(429) == 18


def test_concurrent_cold_start_shares_one_guard(provider, monkeypatch):
    from backend.app.auth import introspection_limits
    original = introspection_limits.IntrospectionGuard
    factory = Mock(side_effect=original)
    monkeypatch.setattr(introspection_limits, "IntrospectionGuard", factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        guards = list(pool.map(lambda _: introspection_limits.get_introspection_guard(), range(20)))
    assert all(guard is guards[0] for guard in guards)
    factory.assert_called_once()


def test_concurrency_exhaustion_does_not_call_provider_and_recovers(protected):
    guard, _, provider = protected
    entered, release = Event(), Event()
    original_open = provider[2].open.return_value
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release blocked provider"
        return original_open
    provider[2].open.side_effect = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(college.introspect, "Bearer first", client_host="198.51.100.1")
        try:
            assert entered.wait(5)
            error = status(lambda: college.introspect("Bearer second", client_host="198.51.100.2"), 503)
            assert error.headers == {"Retry-After": "1"}
            assert provider[2].open.call_count == 1
        finally:
            release.set()
        assert first.result(timeout=5)[1] == "subject-1"
    provider[2].open.side_effect = None
    assert college.introspect("Bearer recovery", client_host="198.51.100.1")[1] == "subject-1"
    with guard.provider_slot():
        pass


@pytest.mark.parametrize("stage", ["open", "read", "json", "oversize", "status", "claims"])
def test_provider_errors_release_slots_and_do_not_leak(protected, stage, caplog):
    guard, _, provider = protected
    if stage == "open":
        provider[2].open.side_effect = TimeoutError("synthetic-secret sensitive-token")
    elif stage == "read":
        provider[3].read.side_effect = TimeoutError("synthetic-secret sensitive-token")
    elif stage == "json":
        provider[3].read.side_effect = lambda length: b"not json"
    elif stage == "oversize":
        provider[3].read.side_effect = lambda length: b"x" * 16385
    elif stage == "status":
        provider[3].status = 500
    else:
        provider[1]["active"] = False
    error = status(lambda: college.introspect("Bearer sensitive-token"), 401 if stage == "claims" else 503)
    assert "sensitive-token" not in str(error.detail) and "sensitive-token" not in caplog.text
    assert "synthetic-secret" not in caplog.text
    with guard.provider_slot():
        pass
    provider[2].open.side_effect = None
    provider[3].status = 200
    provider[1]["active"] = True
    import json
    provider[3].read.side_effect = lambda length: json.dumps(provider[1]).encode()
    assert college.introspect("Bearer recovery")[1] == "subject-1"


def test_configured_timeout_reaches_transport(provider, monkeypatch):
    guard = IntrospectionGuard(replace(IntrospectionSettings(), timeout_seconds=0.25))
    monkeypatch.setattr(college, "get_introspection_guard", lambda: guard)
    college.introspect("Bearer valid")
    assert provider[2].open.call_args.kwargs["timeout"] == 0.25


@pytest.mark.parametrize("key", ["COLLEGE_INTROSPECTION_TIMEOUT_SECONDS", "COLLEGE_INTROSPECTION_MAX_CONCURRENT",
                                "COLLEGE_INTROSPECTION_GLOBAL_PER_MINUTE", "COLLEGE_INTROSPECTION_PEER_PER_MINUTE",
                                "COLLEGE_INTROSPECTION_MAX_PEERS"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid", "2147483648"])
def test_invalid_limits_fail_closed_without_network(provider, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    status(lambda: college.introspect("Bearer valid"), 503)
    provider[2].open.assert_not_called()


def test_environment_defaults_and_overrides(monkeypatch):
    mapping = {"TIMEOUT_SECONDS": "2.5", "MAX_CONCURRENT": "3", "GLOBAL_PER_MINUTE": "90",
               "PEER_PER_MINUTE": "15", "MAX_PEERS": "32"}
    for suffix in mapping:
        monkeypatch.delenv("COLLEGE_INTROSPECTION_" + suffix, raising=False)
    assert IntrospectionSettings.from_environment() == IntrospectionSettings()
    for suffix, value in mapping.items():
        monkeypatch.setenv("COLLEGE_INTROSPECTION_" + suffix, value)
    assert IntrospectionSettings.from_environment() == IntrospectionSettings(2.5, 3, 90, 15, 32)
    monkeypatch.setenv("COLLEGE_INTROSPECTION_TIMEOUT_SECONDS", "61")
    status(IntrospectionSettings.from_environment, 503)


def test_dependency_uses_peer_not_forwarding_headers(protected):
    _, _, provider = protected
    db = Mock()
    db.scalar.return_value = User(role="FACULTY", is_active=True)
    for forwarded in ("198.51.100.1", "198.51.100.2"):
        req = Request({"type": "http", "client": ("192.0.2.1", 123),
                       "headers": [(b"x-forwarded-for", forwarded.encode()),
                                   (b"forwarded", ("for=" + forwarded).encode())]})
        assert get_current_user(req, db=db, authorization="Bearer token").role == "FACULTY"
    status(lambda: get_current_user(req, db=db, authorization="Bearer rotated"), 429)
    assert db.scalar.call_count == 2 and provider[2].open.call_count == 2


def test_http_401_then_429_never_queries_database(protected, monkeypatch):
    _, _, provider = protected
    provider[1]["active"] = False
    db = Mock()
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_db: lambda: db})
    for expected in (401, 401, 429):
        code, body, headers, _ = request(app, [b""], method="GET", path="/auth/me",
                                         headers=[(b"authorization", b"Bearer sensitive-token")])
        assert code == expected and "sensitive-token" not in str(body)
        assert (b"retry-after" in headers) == (expected == 429)
    provider[2].open.assert_called()
    assert provider[2].open.call_count == 2 and db.mock_calls == []


def test_http_provider_timeout_returns_503_without_database_access(protected, monkeypatch):
    _, _, provider = protected
    provider[2].open.side_effect = TimeoutError("sensitive-token")
    db = Mock()
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_db: lambda: db})
    code, body, _, _ = request(app, [b""], method="GET", path="/auth/me",
                                headers=[(b"authorization", b"Bearer sensitive-token")])
    assert (code, body) == (503, {"detail": "College authentication provider is unavailable"})
    assert db.mock_calls == []
