"""Capture actual logging records and bounded ASGI requests; no network/database."""
from contextlib import nullcontext
from io import StringIO
import logging
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
import pytest
from starlette.responses import JSONResponse
from uvicorn.logging import AccessFormatter
from uvicorn.middleware.message_logger import MessageLoggerMiddleware

from backend.app.auth import college
from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db
from backend.app.main import app
from backend.app.models.user import User
from backend.app.routers import asset
from backend.app.secure_logging import SafeAccessLogMiddleware, install_secure_logging
from backend.tests.test_college_auth import provider
from backend.tests.test_request_limits import request


SECRET_VALUES = ("private-nfc-123", "student-private-456", "bearer-secret-789", "college-client-secret")


@pytest.fixture(autouse=True)
def captured_logs(monkeypatch, caplog):
    # Exercise logger filters before every handler; restore global state afterwards.
    for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "filters", list(logger.filters))
        monkeypatch.setattr(logger, "handlers", [caplog.handler])
        monkeypatch.setattr(logger, "propagate", False)
        monkeypatch.setattr(logger, "level", 5)
        monkeypatch.setattr(logger, "disabled", False)
    install_secure_logging()
    caplog.set_level(5)
    return caplog


def assert_no_secrets(capture):
    text = capture.text + repr([record.__dict__ for record in capture.records])
    for value in SECRET_VALUES:
        assert value not in text
    assert "Bearer " not in text and "Authorization" not in text


def request_record(capture):
    records = [record for record in capture.records if record.getMessage().startswith("HTTP request ")]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "duration_ms=" in message
    assert float(message.split("duration_ms=")[1].split()[0]) >= 0
    return message


@pytest.fixture
def api(monkeypatch):
    db = Mock()
    db.no_autoflush = nullcontext()
    actor = User(id=uuid4(), role="FACULTY", is_active=True, college_id="STAFF", full_name="Faculty",
                 email="synthetic@college.invalid")
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides,
                        get_db: lambda: db, get_current_user: lambda: actor})
    return db, actor


@pytest.mark.parametrize("query", [b"nfc_tag_id=private-nfc-123", b"college_id=student-private-456",
                                    b"token=bearer-secret-789&client_secret=college-client-secret",
                                    b"nfc_tag_id=private%2Dnfc%2D123"])
def test_raw_uvicorn_access_record_is_suppressed_before_formatter(query, captured_logs):
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s "%(request_line)s" %(status_code)s'))
    logger = logging.getLogger("uvicorn.access")
    logger.addHandler(handler)
    logger.info('%s - "%s %s HTTP/%s" %d', "forged-peer", "GET", "/assets/lookup?" + query.decode(), "1.1", 200)
    assert stream.getvalue() == "" and captured_logs.records == []


def test_nfc_lookup_logs_route_not_tag_and_preserves_request(api, monkeypatch, captured_logs):
    db, _ = api
    service = Mock(return_value=JSONResponse({"found": True}))
    monkeypatch.setattr(asset, "lookup_asset", service)
    code, body, _, _ = request(MessageLoggerMiddleware(app), [b""], method="GET", path="/assets/lookup",
                                query=b"nfc_tag_id=private-nfc-123", headers=[
                                    (b"authorization", b"Bearer bearer-secret-789"),
                                    (b"cookie", b"college-client-secret"),
                                    (b"x-forwarded-for", b"student-private-456"),
                                    (b"forwarded", b"for=private-nfc-123")])
    assert (code, body) == (200, {"found": True})
    service.assert_called_once_with(db, "private-nfc-123")
    message = request_record(captured_logs)
    assert "method=GET route=/assets/lookup status=200" in message
    assert_no_secrets(captured_logs)


@pytest.mark.parametrize("found", [False, True])
def test_student_lookup_logs_no_identity_in_query_or_response(api, captured_logs, found):
    db, _ = api
    db.scalars.return_value.all.return_value = [User(
        id=uuid4(), college_id="student-private-456", full_name="private-nfc-123",
        role="STUDENT", is_active=True,
    )] if found else []
    code, body, _, _ = request(app, [b""], method="GET", path="/students/lookup",
                                query=b"college_id=student-private-456")
    assert code == (200 if found else 404)
    if found:
        assert body["college_id"] == "student-private-456"
    assert f"route=/students/lookup status={code}" in request_record(captured_logs)
    assert_no_secrets(captured_logs)


@pytest.mark.parametrize("code", [401, 403, 429, 503])
def test_authentication_failures_log_only_metadata(monkeypatch, captured_logs, code):
    def deny():
        raise HTTPException(code, "private-nfc-123")
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides,
                        get_db: lambda: Mock(), get_current_user: deny})
    actual, body, _, _ = request(app, [b""], method="GET", path="/auth/me",
                                  query=b"token=bearer-secret-789", headers=[
                                      (b"authorization", b"Bearer bearer-secret-789")])
    assert actual == code and body["detail"] == "private-nfc-123"
    assert f"route=/auth/me status={code}" in request_record(captured_logs)
    assert_no_secrets(captured_logs)


def test_actual_provider_error_does_not_log_network_credentials(provider, monkeypatch, captured_logs):
    provider[2].open.side_effect = TimeoutError("college-client-secret bearer-secret-789")
    db = Mock()
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_db: lambda: db})
    code, body, _, _ = request(app, [b""], method="GET", path="/auth/me", headers=[
                                (b"authorization", b"Bearer bearer-secret-789")])
    assert (code, body) == (503, {"detail": "College authentication provider is unavailable"})
    assert "status=503" in request_record(captured_logs)
    assert db.mock_calls == []
    assert_no_secrets(captured_logs)


@pytest.mark.parametrize("path", ["/private-nfc-123", "/student-private-456?bearer-secret-789",
                                   "/private-nfc-123\nAuthorization:bearer-secret-789"])
def test_unmatched_paths_never_enter_access_logs(captured_logs, path):
    code, _, _, _ = request(app, [b""], method="GET", path=path)
    assert code == 404
    assert "route=<unmatched> status=404" in request_record(captured_logs)
    assert_no_secrets(captured_logs)


def test_dynamic_identifier_and_validation_input_are_not_logged(api, captured_logs):
    code, _, _, _ = request(app, [b""], method="GET", path="/assets/private-nfc-123")
    assert code == 422
    assert "route=/assets/{asset_id} status=422" in request_record(captured_logs)
    assert_no_secrets(captured_logs)


@pytest.mark.parametrize("code,headers", [(413, [(b"content-length", b"999999999")]),
                                          (400, [(b"content-length", b"private-nfc-123")])])
def test_rejections_before_routing_are_logged_safely(captured_logs, code, headers):
    actual, _, _, _ = request(app, [], headers=headers, path="/private-nfc-123", query=b"token=bearer-secret-789")
    assert actual == code
    assert f"route=<unmatched> status={code}" in request_record(captured_logs)
    assert_no_secrets(captured_logs)


def test_unhandled_exception_keeps_type_and_location_without_exception_values(captured_logs):
    target = FastAPI()
    target.add_middleware(SafeAccessLogMiddleware)
    @target.get("/failure")
    def failure():
        try:
            raise ValueError("student-private-456")
        except ValueError:
            raise RuntimeError("private-nfc-123 college-client-secret")
    with pytest.raises(RuntimeError) as error:
        request(target, [b""], method="GET", path="/failure", query=b"token=bearer-secret-789")
    # Model Uvicorn's actual exception logging boundary after ASGI propagation.
    logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=error.value)
    assert "status=500" in request_record(captured_logs)
    assert "failed=True" in captured_logs.text
    diagnostic = captured_logs.records[-1]
    assert "type=RuntimeError" in diagnostic.getMessage()
    assert "test_secure_logging.py:" in diagnostic.getMessage()
    assert ":failure" in diagnostic.getMessage()
    assert diagnostic.exc_info is None and diagnostic.exc_text is None
    assert_no_secrets(captured_logs)


@pytest.mark.parametrize("level,message,args", [
    (logging.ERROR, "ASGI callable returned %s", ("bearer-secret-789",)),
    (logging.ERROR, "Error loading application college-client-secret", ()),
    (logging.DEBUG, "Authorization: %s", ("Bearer bearer-secret-789",)),
    (logging.INFO, '%s - "WebSocket %s" [accepted]', ("untrusted-peer", "/?nfc=private-nfc-123")),
])
def test_other_server_diagnostics_cannot_emit_sensitive_values(captured_logs, level, message, args):
    logging.getLogger("uvicorn.error").log(level, message, *args)
    assert_no_secrets(captured_logs)
    if level == logging.ERROR:
        assert "ASGI server error (details withheld)" in captured_logs.text
    else:
        assert captured_logs.records == []


def test_normal_operational_server_message_is_preserved(captured_logs):
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 123)
    assert "Started server process [123]" in captured_logs.text


def test_empty_exception_info_does_not_break_diagnostics(captured_logs):
    logging.getLogger("uvicorn.error").error("private-nfc-123", exc_info=True)
    assert "type=Unknown" in captured_logs.text
    assert_no_secrets(captured_logs)


def test_programmatic_server_setup_restores_filters(monkeypatch):
    for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        monkeypatch.setattr(logging.getLogger(name), "filters", [])
    SafeAccessLogMiddleware(Mock())
    for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        assert len(logging.getLogger(name).filters) == 1


def test_installation_is_idempotent_and_main_wraps_body_limit():
    from backend.app.request_limits import RequestSizeLimitMiddleware
    install_secure_logging()
    install_secure_logging()
    for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        assert sum(bool(getattr(item, "inventory_safe_filter", False))
                   for item in logging.getLogger(name).filters) == 1
    classes = [middleware.cls for middleware in app.user_middleware]
    assert classes.index(SafeAccessLogMiddleware) < classes.index(RequestSizeLimitMiddleware)
