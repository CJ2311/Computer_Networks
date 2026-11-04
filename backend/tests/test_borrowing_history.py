import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy.dialects import postgresql

from backend.app.database import get_db
from backend.app.models.user import User
from backend.app.routers.checkout import router
from backend.app.schemas.borrowing_history import HistoryFilters
from backend.app.services.borrowing_history import get_borrowing_history, list_borrowing_history


@pytest.fixture
def history():
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    user = User(id=uuid4(), role="STUDENT", is_active=True)
    row = dict(id=uuid4(), asset_id=uuid4(), equipment_id=uuid4(), asset_code="TEST-01",
               equipment_name="Sensor", identifier_snapshot_status="CAPTURED", student_id=user.id, issued_by=uuid4(),
               checked_out_at=now - timedelta(days=1), due_at=now + timedelta(days=1),
               returned_at=None, returned_to=None, status="BORROWED",
               is_currently_overdue=False, was_returned_late=False)
    db = Mock()
    db.no_autoflush = nullcontext()
    db.scalar.return_value = now
    db.execute.return_value.mappings.return_value.all.return_value = [row]
    db.execute.return_value.mappings.return_value.one_or_none.return_value = row
    return db, user, row, now


def compiled_query(db):
    return db.execute.call_args.args[0].compile(dialect=postgresql.dialect())


def assert_read_only(db):
    for method in ("add", "delete", "flush", "commit", "refresh"):
        getattr(db, method).assert_not_called()
    for call in db.execute.call_args_list:
        assert call.args[0].is_select
        assert "FOR UPDATE" not in str(call.args[0])


@pytest.mark.parametrize("detail", [False, True])
def test_student_scope_is_in_sql_and_clock_is_read_once(history, detail):
    db, user, row, now = history
    response = get_borrowing_history(db, user, row["id"]) if detail else list_borrowing_history(db, user, HistoryFilters())
    statement = compiled_query(db)
    assert "equipment_checkouts.student_id =" in str(statement)
    assert user.id in statement.params.values()
    assert response.as_of == now
    assert "clock_timestamp()" in str(db.scalar.call_args.args[0])
    db.scalar.assert_called_once()
    assert now in statement.params.values()
    assert_read_only(db)
    serialized = response.model_dump()
    item = serialized if detail else serialized["items"][0]
    assert not {"notes", "email", "full_name", "college_id"} & item.keys()


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY", "LAB_ASSISTANT"])
@pytest.mark.parametrize("detail", [False, True])
def test_staff_have_unrestricted_history_scope(history, role, detail):
    db, user, row, _ = history
    user.role = role
    if detail:
        get_borrowing_history(db, user, row["id"])
    else:
        list_borrowing_history(db, user, HistoryFilters())
    assert "equipment_checkouts.student_id =" not in str(compiled_query(db))
    assert_read_only(db)


def test_student_foreign_borrower_filter_is_rejected_before_query(history):
    db, user, _, _ = history
    with pytest.raises(HTTPException) as error:
        list_borrowing_history(db, user, HistoryFilters(student_id=uuid4()))
    assert error.value.status_code == 403
    db.scalar.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize("role", ["STUDENT", "ADMIN", "FACULTY", "LAB_ASSISTANT", "UNKNOWN"])
def test_inactive_and_unknown_users_rejected(history, role):
    db, user, row, _ = history
    user.role, user.is_active = role, role == "UNKNOWN"
    for operation in (
        lambda: list_borrowing_history(db, user, HistoryFilters()),
        lambda: get_borrowing_history(db, user, row["id"]),
    ):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 403
    db.scalar.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize("role", ["STUDENT", "FACULTY"])
def test_borrower_filter_and_all_other_filters_are_bound_in_sql(history, role):
    db, user, row, now = history
    user.role = role
    target = user.id if role == "STUDENT" else uuid4()
    filters = HistoryFilters(
        equipment_id=row["equipment_id"], asset_id=row["asset_id"], student_id=target,
        status="BORROWED", overdue=True, checked_out_from=now - timedelta(days=2),
        checked_out_before=now, limit=100, offset=3,
    )
    list_borrowing_history(db, user, filters)
    query = compiled_query(db)
    sql, values = str(query), list(query.params.values())
    assert target in values and row["asset_id"] in values and row["equipment_id"] in values
    assert filters.checked_out_from in values and now in values
    assert "equipment_checkouts.checked_out_at >=" in sql
    assert "equipment_checkouts.checked_out_at <" in sql
    assert "ORDER BY equipment_checkouts.checked_out_at DESC, equipment_checkouts.id DESC" in sql
    assert "IS true" in sql
    assert 101 in values and 3 in values
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
    assert_read_only(db)


@pytest.mark.parametrize("overdue", [True, False])
def test_overdue_uses_strict_database_deadline_and_separate_historical_flag(history, overdue):
    db, user, _, _ = history
    list_borrowing_history(db, user, HistoryFilters(overdue=overdue))
    query = compiled_query(db)
    sql = str(query)
    assert "equipment_checkouts.due_at <" in sql
    assert "equipment_checkouts.due_at <=" not in sql
    assert "equipment_checkouts.returned_at > equipment_checkouts.due_at" in sql
    assert "is_currently_overdue" in sql and "was_returned_late" in sql
    assert "IS " + str(overdue).lower() in sql
    assert "BORROWED" in query.params.values() and "RETURNED" in query.params.values()


@pytest.mark.parametrize("count, limit, more", [(0, 25, False), (1, 1, False), (2, 1, True)])
def test_bounded_pagination_and_empty_pages(history, count, limit, more):
    db, user, row, _ = history
    db.execute.return_value.mappings.return_value.all.return_value = [row] * count
    result = list_borrowing_history(db, user, HistoryFilters(limit=limit, offset=4))
    assert len(result.items) == min(count, limit)
    assert result.has_more is more
    assert result.limit == limit and result.offset == 4
    assert limit + 1 in compiled_query(db).params.values()


def test_missing_and_foreign_student_records_have_the_same_404(history):
    db, user, _, _ = history
    db.execute.return_value.mappings.return_value.one_or_none.return_value = None
    with pytest.raises(HTTPException) as error:
        get_borrowing_history(db, user, uuid4())
    assert error.value.status_code == 404
    assert error.value.detail == "Checkout not found"
    assert "equipment_checkouts.student_id =" in str(compiled_query(db))


@pytest.mark.parametrize("data", [
    {"equipment_id": "invalid"}, {"asset_id": "invalid"}, {"student_id": "invalid"},
    {"status": "OVERDUE"}, {"overdue": "perhaps"}, {"limit": 0}, {"limit": 101},
    {"limit": 1.5}, {"offset": -1}, {"offset": "x"}, {"offset": 2**63},
    {"checked_out_from": "2026-01-01T00:00:00"}, {"checked_out_before": "2026-01-01"},
    {"checked_out_from": "2026-02-01T00:00:00Z", "checked_out_before": "2026-01-01T00:00:00Z"},
    {"checked_out_from": "2026-01-01T00:00:00Z", "checked_out_before": "2026-01-01T00:00:00Z"},
    {"unknown": "value"},
])
def test_filter_validation(data):
    with pytest.raises(ValidationError):
        HistoryFilters.model_validate(data)


def test_timezone_offsets_normalize_to_utc():
    filters = HistoryFilters(checked_out_from="2026-11-02T17:30:00+05:30")
    assert filters.checked_out_from == datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    assert filters.checked_out_from.tzinfo == timezone.utc


@pytest.fixture
def history_http(history, monkeypatch):
    db, user, row, now = history
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)

    def sessions():
        yield db

    app.dependency_overrides[get_db] = sessions

    def request(query=None, detail=False, header="DEV-STUDENT", path_id=None):
        # Authentication uses the actual existing dependency and a mocked DB lookup.
        db.scalar.side_effect = [user, now]

        async def run():
            path = f"/checkouts/{path_id or row['id']}" if detail else "/checkouts"
            headers = [] if header is None else [(b"x-dev-user", header.encode())]
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
                     "query_string": urlencode(query or {}).encode(), "root_path": "", "headers": headers,
                     "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80)}
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await app(scope, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
            return status, body

        return asyncio.run(run())

    return request


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("role, header", [("STUDENT", "DEV-STUDENT"), ("FACULTY", "DEV-FACULTY"),
                                         ("LAB_ASSISTANT", "DEV-ASSISTANT"), ("ADMIN", "DEV-ADMIN")])
def test_http_permissions_and_read_only(history, history_http, detail, role, header):
    db, user, _, _ = history
    user.role = role
    status, response = history_http(detail=detail, header=header)
    assert status == 200
    assert "as_of" in response
    assert_read_only(db)


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("header, active, expected", [(None, True, 401), ("DEV-UNKNOWN", True, 401),
                                                    ("DEV-STUDENT", False, 403)])
def test_http_rejects_missing_invalid_and_inactive_auth(history, history_http, detail, header, active, expected):
    db, user, _, _ = history
    user.is_active = active
    assert history_http(detail=detail, header=header)[0] == expected
    db.execute.assert_not_called()


@pytest.mark.parametrize("query", [
    {"limit": 101}, {"offset": -1}, {"offset": str(2**63)}, {"equipment_id": "invalid"}, {"student_id": "invalid"},
    {"status": "invalid"}, {"overdue": "invalid"}, {"checked_out_from": "2026-01-01"},
    {"checked_out_from": "2026-02-01T00:00:00Z", "checked_out_before": "2026-01-01T00:00:00Z"},
    {"unknown": "value"},
])
def test_http_filter_errors_are_422(history, history_http, query):
    assert history_http(query)[0] == 422
    history[0].execute.assert_not_called()


def test_http_foreign_borrower_query_is_403(history, history_http):
    assert history_http({"student_id": str(uuid4())})[0] == 403
    history[0].execute.assert_not_called()


def test_http_foreign_and_missing_details_are_404(history, history_http):
    history[0].execute.return_value.mappings.return_value.one_or_none.return_value = None
    assert history_http(detail=True)[0] == 404


def test_http_invalid_path_id_is_422(history, history_http):
    assert history_http(detail=True, path_id="invalid")[0] == 422
    history[0].execute.assert_not_called()


def test_history_openapi_preserves_existing_post_contracts():
    from backend.app.main import app

    spec = app.openapi()
    assert spec["paths"]["/checkouts"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/HistoryPage")
    assert spec["paths"]["/checkouts/{checkout_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/HistoryDetail")
    assert spec["paths"]["/checkouts"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/CheckoutResponse")
    assert spec["paths"]["/checkouts/{checkout_id}/return"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/ReturnResponse")
