"""Bounded ASGI requests and validation checks without database connections."""
import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
import pytest
from starlette.responses import JSONResponse

from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db, get_inventory_write_db
from backend.app.main import app
from backend.app.models.user import User
from backend.app.request_limits import (
    DEFAULT_MAX_REQUEST_BODY_BYTES, RequestSizeLimitMiddleware, get_max_request_body_bytes,
)
from backend.app.routers import asset, checkout, equipment, maintenance, reports
from backend.app.schemas.equipment import EquipmentCreate, EquipmentUpdate
from backend.tests.registration_helpers import equipment_payload


def request(target, chunks, *, headers=(), method="POST", path="/equipment", query=b""):
    async def run():
        emitted = []
        received = 0

        async def receive():
            nonlocal received
            if received >= len(chunks):
                await asyncio.Event().wait()
            chunk = chunks[received]
            received += 1
            await asyncio.sleep(0)
            if isinstance(chunk, dict):
                return chunk
            return {"type": "http.request", "body": chunk, "more_body": received < len(chunks)}

        async def send(message):
            emitted.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": query, "root_path": "", "headers": [(b"content-type", b"application/json"), *headers],
                 "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 80)}
        await asyncio.wait_for(target(scope, receive, send), timeout=10)
        starts = [message for message in emitted if message["type"] == "http.response.start"]
        body = b"".join(message.get("body", b"") for message in emitted if message["type"] == "http.response.body")
        response_headers = dict(starts[0]["headers"]) if starts else {}
        response_body = None
        if body:
            if response_headers.get(b"content-type", b"").startswith(b"application/json"):
                response_body = json.loads(body)
            else:
                response_body = body.decode()
        return (starts[0]["status"] if starts else None, response_body, response_headers, received)
    return asyncio.run(run())


@pytest.fixture
def echo():
    async def respond(scope, receive, send):
        message = await receive()
        assert message["more_body"] is False
        await JSONResponse({"body": message["body"].hex()})(scope, receive, send)
    return AsyncMock(side_effect=respond)


@pytest.mark.parametrize("size", [0, 7, 8])
@pytest.mark.parametrize("declared", [False, True])
def test_body_at_or_below_limit_is_replayed_unchanged(echo, size, declared):
    body = b"a" * size
    headers = [(b"content-length", str(size).encode())] if declared else []
    status, result, _, received = request(RequestSizeLimitMiddleware(echo, 8), [body], headers=headers)
    assert (status, result, received) == (200, {"body": body.hex()}, 1)
    echo.assert_awaited_once()


@pytest.mark.parametrize("headers", [[], [(b"transfer-encoding", b"chunked")], [(b"content-length", b"1")]])
@pytest.mark.parametrize("chunks", [[b"x" * 9], [b"1234", b"5678", b"9"], [b""] * 3 + [b"x"] * 9])
def test_actual_streamed_bytes_enforce_limit_without_trusting_headers(echo, headers, chunks):
    status, result, _, _ = request(RequestSizeLimitMiddleware(echo, 8), chunks, headers=headers)
    assert (status, result) == (413, {"detail": "Request body too large"})
    echo.assert_not_awaited()


def test_exact_stream_boundary_and_utf8_byte_accounting(echo):
    body = "😀😀".encode()
    assert len(body) == 8
    assert request(RequestSizeLimitMiddleware(echo, 8), [body[:3], body[3:]])[:2] == (200, {"body": body.hex()})
    echo.reset_mock()
    assert request(RequestSizeLimitMiddleware(echo, 7), [body])[:2] == (413, {"detail": "Request body too large"})
    echo.assert_not_awaited()


@pytest.mark.parametrize("length", [b"9", b"0009", b"9" * 5000])
def test_declared_oversize_is_rejected_without_reading_any_body(echo, length):
    status, result, _, received = request(RequestSizeLimitMiddleware(echo, 8), [b""],
                                          headers=[(b"content-length", length)])
    assert (status, result, received) == (413, {"detail": "Request body too large"}, 0)
    echo.assert_not_awaited()


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"-1")], [(b"content-length", b"1.5")], [(b"content-length", b"")],
    [(b"content-length", b"abc")], [(b"content-length", b"8,9")],
    [(b"content-length", b"1"), (b"content-length", b"1")],
    [(b"content-length", b"1"), (b"content-length", b"9")],
])
def test_malformed_or_duplicate_lengths_fail_predictably(echo, headers):
    status, body, _, received = request(RequestSizeLimitMiddleware(echo, 8), [b"x"], headers=headers)
    assert (status, body, received) == (400, {"detail": "Invalid Content-Length"}, 0)
    echo.assert_not_awaited()


def test_zero_padded_length_is_accepted_and_oversized_stream_stops_early(echo):
    assert request(RequestSizeLimitMiddleware(echo, 8), [b"x"], headers=[(b"content-length", b"0001")])[0] == 200
    echo.reset_mock()
    result = request(RequestSizeLimitMiddleware(echo, 8), [b"1234", b"56789", b"unread"])
    assert result[0] == 413 and result[3] == 2
    echo.assert_not_awaited()


def test_disconnect_before_complete_body_never_routes(echo):
    result = request(RequestSizeLimitMiddleware(echo, 8), [b"a", {"type": "http.disconnect"}])
    assert result[:2] == (None, None)
    echo.assert_not_awaited()


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_are_delegated(scope_type):
    downstream, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    scope = {"type": scope_type}
    asyncio.run(RequestSizeLimitMiddleware(downstream, 8)(scope, receive, send))
    downstream.assert_awaited_once_with(scope, receive, send)
    receive.assert_not_awaited()


def test_default_and_configured_limit(monkeypatch):
    monkeypatch.delenv("INVENTORY_MAX_REQUEST_BODY_BYTES", raising=False)
    assert get_max_request_body_bytes() == DEFAULT_MAX_REQUEST_BODY_BYTES == 8_388_608
    monkeypatch.setenv("INVENTORY_MAX_REQUEST_BODY_BYTES", " 1234 ")
    assert get_max_request_body_bytes() == 1234


@pytest.mark.parametrize("value", ["0", "-1", "", "1.5", "false", "1_000", "９９", "9" * 5000])
def test_invalid_configuration_fails_closed(monkeypatch, value):
    monkeypatch.setenv("INVENTORY_MAX_REQUEST_BODY_BYTES", value)
    with pytest.raises(ValueError, match="positive integer"):
        get_max_request_body_bytes()


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_middleware_requires_positive_integer_limit(value):
    with pytest.raises(ValueError, match="positive integer"):
        RequestSizeLimitMiddleware(AsyncMock(), value)


@pytest.mark.parametrize("method", ["POST", "OPTIONS"])
@pytest.mark.parametrize("origin", ["https://college.invalid", "https://unapproved.invalid"])
def test_cors_cannot_bypass_limit_and_allowed_origins_receive_readable_413(method, origin):
    target = FastAPI()
    target.add_middleware(CORSMiddleware, allow_origins=["https://college.invalid"])
    target.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=8, allowed_origins=["https://college.invalid"])
    status, _, headers, _ = request(target, [b"x" * 4, b"x" * 5], method=method, headers=[
        (b"origin", origin.encode()), (b"access-control-request-method", b"POST"),
        (b"transfer-encoding", b"chunked"),
    ])
    assert status == 413
    if origin == "https://college.invalid":
        assert headers[b"access-control-allow-origin"] == origin.encode()
        assert headers[b"vary"] == b"Origin"
    else:
        assert b"access-control-allow-origin" not in headers


def test_normal_cors_preflight_still_succeeds():
    target = FastAPI()
    target.add_middleware(CORSMiddleware, allow_origins=["https://college.invalid"], allow_methods=["POST"])
    target.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=8, allowed_origins=["https://college.invalid"])
    status, _, headers, _ = request(target, [b""], method="OPTIONS", headers=[
        (b"origin", b"https://college.invalid"), (b"access-control-request-method", b"POST"),
    ])
    assert status == 200 and headers[b"access-control-allow-origin"] == b"https://college.invalid"


def test_body_limit_is_installed_before_cors_preflight_in_real_application():
    classes = [middleware.cls for middleware in app.user_middleware]
    assert classes.index(RequestSizeLimitMiddleware) < classes.index(CORSMiddleware)


@pytest.fixture
def api(monkeypatch):
    db = Mock()
    actor = User(id=uuid4(), college_id="F2", full_name="Faculty", email="f2@college.invalid",
                 role="FACULTY", is_active=True)
    def sessions():
        yield db
    overrides = {**app.dependency_overrides, get_db: sessions, get_inventory_write_db: sessions,
                 get_current_user: lambda: actor}
    monkeypatch.setattr(app, "dependency_overrides", overrides)
    return app, db


@pytest.mark.parametrize("headers", [[], [(b"transfer-encoding", b"chunked")], [(b"content-length", b"1")]])
def test_real_application_rejects_oversize_before_any_dependencies_or_mutations(api, monkeypatch, headers):
    target, db = api
    limit = next(m.kwargs["max_body_bytes"] for m in target.user_middleware if m.cls is RequestSizeLimitMiddleware)
    service = Mock()
    monkeypatch.setattr(equipment, "create_equipment", service)
    prefix = json.dumps(equipment_payload()).encode()
    status, body, _, _ = request(target, [prefix, b" " * (limit + 1)], headers=headers)
    assert (status, body) == (413, {"detail": "Request body too large"})
    service.assert_not_called()
    assert db.mock_calls == []


@pytest.mark.parametrize("method,path,service_name", [
    ("POST", "/equipment", "create_equipment"),
    ("PATCH", "/equipment/" + str(uuid4()), "update_equipment"),
    ("POST", "/equipment/bulk", "create_equipment_bulk"),
])
def test_description_over_limit_rejects_entire_request_before_service(api, monkeypatch, method, path, service_name):
    target, db = api
    service = Mock()
    monkeypatch.setattr(equipment, service_name, service)
    payload = equipment_payload(description="x" * 4001)
    if method == "PATCH":
        payload = {"description": payload["description"]}
    if path.endswith("/bulk"):
        payload = {"items": [equipment_payload(), payload]}
    status, body, _, _ = request(target, [json.dumps(payload).encode()], method=method, path=path)
    assert status == 422 and body["detail"][0]["type"] == "string_too_long"
    service.assert_not_called()
    assert db.mock_calls == []


@pytest.mark.parametrize("schema", [EquipmentCreate, EquipmentUpdate])
@pytest.mark.parametrize("size", [0, 3999, 4000, 4001])
def test_description_character_boundaries(schema, size):
    payload = {"description": "é" * size}
    if schema is EquipmentCreate:
        payload = equipment_payload(**payload)
    if size > 4000:
        with pytest.raises(ValidationError):
            schema.model_validate(payload)
    else:
        assert schema.model_validate(payload).description == "é" * size


def test_maximum_escaped_unicode_bulk_registration_fits_default_limit(api, monkeypatch):
    target, db = api
    item = equipment_payload(name="😀" * 150, category="😀" * 100, default_location="😀" * 150,
                             manufacturer="😀" * 100, model_number="😀" * 100, description="😀" * 4000)
    payload = {"items": [item] * 100}
    body = json.dumps(payload, ensure_ascii=True).encode()
    assert len(body) < DEFAULT_MAX_REQUEST_BODY_BYTES
    response = {**item, "id": str(uuid4()), "purchase_price": None,
                "created_at": datetime.now(timezone.utc).isoformat()}
    service = Mock(return_value=[response] * 100)
    monkeypatch.setattr(equipment, "create_equipment_bulk", service)
    status, result, _, _ = request(target, [body[:1000], body[1000:]], path="/equipment/bulk")
    assert status == 201 and len(result) == 100
    assert result[0]["description"] == item["description"]
    assert len(service.call_args.args[1]) == 100
    assert db.mock_calls == []


@pytest.mark.parametrize("path,parameter,service_name", [
    ("/reports/inventory/categories", "category", "get_category_report"),
    ("/reports/inventory/stock", "category", "get_stock_report"),
    ("/reports/inventory/stock", "search", "get_stock_report"),
    ("/reports/inventory/missing", "category", "get_missing_report"),
])
@pytest.mark.parametrize("size", [100, 101])
def test_report_filter_boundaries_before_query(api, monkeypatch, path, parameter, service_name, size):
    target, db = api
    service = Mock(return_value=JSONResponse({"accepted": True}))
    monkeypatch.setattr(reports, service_name, service)
    query = urlencode({parameter: "x" * size}).encode()
    status, body, _, _ = request(target, [b""], method="GET", path=path, query=query)
    if size == 100:
        assert (status, body) == (200, {"accepted": True})
        service.assert_called_once()
    else:
        assert status == 422 and body["detail"][0]["type"] == "string_too_long"
        service.assert_not_called()
    assert db.mock_calls == []


@pytest.mark.parametrize("module,service_name,path,payload,query,method,status", [
    (equipment, "create_equipment", "/equipment", equipment_payload(description="é" * 4000), b"", "POST", 201),
    (asset, "create_asset", "/assets", {"equipment_id": str(uuid4()), "asset_code": "ASSET", "location": "Lab"}, b"", "POST", 201),
    (asset, "create_asset_bulk", "/assets/bulk", {"items": [
        {"equipment_id": str(uuid4()), "asset_code": "ASSET", "location": "Lab"}]}, b"", "POST", 201),
    (checkout, "create_checkout", "/checkouts", {"asset_id": str(uuid4()), "student_id": str(uuid4()),
        "due_at": "2030-01-01T00:00:00Z", "notes": "x" * 1000}, b"", "POST", 201),
    (checkout, "record_return", "/checkouts/" + str(uuid4()) + "/return",
        {"condition_status": "WORKING", "notes": "x" * 1000}, b"", "POST", 200),
    (maintenance, "report_maintenance_issue", "/maintenance", {"asset_id": str(uuid4()),
        "issue_type": "DAMAGE", "issue_description": "x" * 4000}, b"", "POST", 201),
    (asset, "lookup_asset", "/assets/lookup", None, b"nfc_tag_id=" + b"n" * 100, "GET", 200),
])
def test_normal_workflows_reach_existing_services_through_stream_limiter(
    api, monkeypatch, module, service_name, path, payload, query, method, status,
):
    target, db = api
    service = Mock(return_value=JSONResponse({"accepted": True}, status_code=status))
    monkeypatch.setattr(module, service_name, service)
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
    result = request(target, [body[:5], body[5:]], method=method, path=path, query=query,
                     headers=[(b"content-length", str(len(body)).encode())])
    assert result[:2] == (status, {"accepted": True})
    service.assert_called_once()
    assert db.mock_calls == []


def test_existing_long_descriptions_remain_readable(api):
    target, db = api
    description = "Historical description " * 300
    record = {**equipment_payload(description=description), "id": str(uuid4()),
              "manufacturer": None, "model_number": None, "purchase_price": None,
              "created_at": datetime.now(timezone.utc).isoformat()}
    db.get.return_value = record
    status, result, _, _ = request(target, [b""], method="GET", path="/equipment/" + record["id"])
    assert status == 200 and result["description"] == description


def test_openapi_documents_input_bounds_without_restricting_historical_responses():
    schema = app.openapi()
    models = schema["components"]["schemas"]
    for name in ("EquipmentCreate", "EquipmentUpdate"):
        assert {"type": "string", "maxLength": 4000} in models[name]["properties"]["description"]["anyOf"]
    assert models["EquipmentResponse"]["properties"]["description"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    for path in ("/reports/inventory/categories", "/reports/inventory/stock", "/reports/inventory/missing"):
        parameters = schema["paths"][path]["get"]["parameters"]
        for parameter in parameters:
            if parameter["name"] in {"category", "search"}:
                assert {"type": "string", "maxLength": 100} in parameter["schema"]["anyOf"]
