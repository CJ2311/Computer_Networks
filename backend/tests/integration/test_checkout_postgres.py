from backend.tests.integration.inventory_helpers import database_now, label_transaction
"""Real PostgreSQL tests; never fall back to the application's database."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import UUID, uuid4
from urllib.parse import urlencode

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import FastAPI, HTTPException, Request
import pytest
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.orm import Session

from backend.app.database import Base, get_db, get_inventory_write_db
from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.routers.checkout import router
from backend.app.services.checkout import create_checkout
from backend.app.services.checkout import record_return
from backend.app.schemas.checkout import ReturnCreate
from backend.app.schemas.borrowing_history import HistoryFilters
from backend.app.services.borrowing_history import get_borrowing_history, list_borrowing_history


@pytest.fixture
def records(database):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        users = {}
        for role, college_id in (
            ("ADMIN", "DEV-ADMIN"), ("FACULTY", "DEV-FACULTY"),
            ("LAB_ASSISTANT", "DEV-ASSISTANT"), ("STUDENT", "DEV-STUDENT"),
        ):
            user = User(id=uuid4(), college_id=college_id, full_name="Integration test",
                        email=college_id + "@example.invalid", role=role, is_active=True)
            db.add(user)
            users[role] = user.id
        equipment = Equipment(id=uuid4(), name="Test sensor", category="IoT",
                              tracking_type="INDIVIDUAL", default_location="Test lab")
        db.add(equipment)
        db.flush()
        asset = EquipmentAsset(id=uuid4(), equipment_id=equipment.id,
                               asset_code="TEST-001", location="Test lab")
        db.add(asset)
        db.commit()
        return {"users": users, "asset": asset.id}


@pytest.fixture
def api(database, monkeypatch):
    # Exercise the actual router and auth dependencies with only get_db replaced.
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)
    app = FastAPI()
    app.include_router(router)

    def sessions(request: Request):
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            if request.method == "GET":
                db.execute(text("SET TRANSACTION READ ONLY"))
            yield db

    def write_sessions():
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            yield db

    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_inventory_write_db] = write_sessions

    def request(payload=None, dev_user="DEV-ASSISTANT", path="/checkouts", method="POST", query=None):
        async def run():
            body = json.dumps(payload).encode()
            headers = [(b"content-type", b"application/json")]
            if dev_user is not None:
                headers.append((b"x-dev-user", dev_user.encode()))
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": path,
                     "raw_path": path.encode(), "query_string": urlencode(query or {}, doseq=True).encode(), "root_path": "",
                     "headers": headers, "client": ("127.0.0.1", 12345),
                     "server": ("127.0.0.1", 80)}
            messages = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            await app(scope, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            response = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return status, json.loads(response)

        return asyncio.run(run())

    return request


def payload(records):
    return {"asset_id": str(records["asset"]), "student_id": str(records["users"]["STUDENT"]),
            "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "notes": "PostgreSQL integration test"}


def assert_no_checkout(database, asset_id, availability="AVAILABLE"):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        assert db.scalar(select(func.count()).select_from(EquipmentCheckout)) == 0
        assert db.get(EquipmentAsset, asset_id).availability_status == availability


def test_migrated_schema_matches_model(database):
    with database.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(context, Base.metadata) == []
        inspector = inspect(connection)
        assert inspector.get_pk_constraint("equipment_checkouts")["constrained_columns"] == ["id"]
        assert {c["name"] for c in inspector.get_check_constraints("equipment_checkouts")} == {
            "ck_checkout_status", "ck_checkout_return_consistency",
            "ck_checkout_due_after_issue", "ck_checkout_return_after_issue",
            "ck_checkout_identifier_snapshots",
        }


@pytest.mark.parametrize("issuer", ["DEV-ADMIN", "DEV-FACULTY", "DEV-ASSISTANT"])
def test_success_and_persistence(database, records, api, issuer):
    data = payload(records)
    status, response = api(data, issuer)
    assert status == 201
    from uuid import UUID
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        checkout = db.get(EquipmentCheckout, UUID(response["id"]))
        assert checkout is not None
        assert checkout.asset_id == records["asset"]
        assert checkout.student_id == records["users"]["STUDENT"]
        assert checkout.issued_by == records["users"][{"DEV-ADMIN": "ADMIN", "DEV-FACULTY": "FACULTY", "DEV-ASSISTANT": "LAB_ASSISTANT"}[issuer]]
        assert checkout.status == response["status"] == "BORROWED"
        assert checkout.notes == data["notes"]
        assert checkout.due_at == datetime.fromisoformat(data["due_at"])
        assert checkout.checked_out_at.tzinfo is not None
        assert checkout.returned_at is None and checkout.returned_to is None
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "BORROWED"


@pytest.mark.parametrize("availability", ["BORROWED", "RESERVED", "RETIRED"])
def test_unavailable_asset(database, records, api, availability):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        db.get(EquipmentAsset, records["asset"]).availability_status = availability
        db.commit()
    assert api(payload(records))[0] == 409
    assert_no_checkout(database, records["asset"], availability)


@pytest.mark.parametrize("borrower, expected", [("missing", 404), ("staff", 400), ("inactive", 403)])
def test_invalid_borrower(database, records, api, borrower, expected):
    data = payload(records)
    if borrower == "missing":
        data["student_id"] = str(uuid4())
    elif borrower == "staff":
        data["student_id"] = str(records["users"]["FACULTY"])
    else:
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            db.get(User, records["users"]["STUDENT"]).is_active = False
            db.commit()
    assert api(data)[0] == expected
    assert_no_checkout(database, records["asset"])


@pytest.mark.parametrize("dev_user, expected", [(None, 401), ("DEV-UNKNOWN", 401), ("DEV-STUDENT", 403)])
def test_auth_and_unauthorized_role(database, records, api, dev_user, expected):
    assert api(payload(records), dev_user)[0] == expected
    assert_no_checkout(database, records["asset"])


def test_inactive_issuer(database, records, api):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        db.get(User, records["users"]["LAB_ASSISTANT"]).is_active = False
        db.commit()
    assert api(payload(records))[0] == 403
    assert_no_checkout(database, records["asset"])


def test_fabricated_issuer_rejected_without_writes(database, records):
    # Caller-supplied roles cannot authorize an unpersisted issuer.
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        issuer = User(id=uuid4(), role="ADMIN", is_active=True)
        with pytest.raises(HTTPException) as error:
            create_checkout(db, records["asset"], records["users"]["STUDENT"], issuer,
                            datetime.now(timezone.utc) + timedelta(days=1))
        assert error.value.status_code == 403
        assert db.scalar(select(func.count()).select_from(EquipmentCheckout)) == 0
    assert_no_checkout(database, records["asset"])


def test_concurrent_checkout_only_one_succeeds(database, records):
    label = "checkout_race_" + uuid4().hex

    def attempt():
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            label_transaction(db, label)
            staff = User(id=records["users"]["LAB_ASSISTANT"])
            try:
                checkout = create_checkout(db, records["asset"], records["users"]["STUDENT"], staff,
                                           datetime.now(timezone.utc) + timedelta(days=1))
                return 201, checkout.id
            except HTTPException as error:
                return error.status_code, None

    # Hold the row until PostgreSQL confirms both workers are actually waiting.
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        transaction = blocker.begin()
        blocker.execute(select(EquipmentAsset).where(EquipmentAsset.id == records["asset"]).with_for_update())
        futures = [pool.submit(attempt) for _ in range(2)]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = :label AND wait_event_type = 'Lock'"
                    ), {"label": label})
                if waiting == 2:
                    break
                time.sleep(0.05)
            assert waiting == 2, "Both workers must contend for the same PostgreSQL row lock"
        finally:
            transaction.rollback()
        results = [future.result(timeout=15) for future in futures]
    assert sorted(status for status, _ in results) == [201, 409]
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        checkouts = db.scalars(select(EquipmentCheckout)).all()
        assert len(checkouts) == 1
        assert checkouts[0].id == next(checkout_id for status, checkout_id in results if status == 201)
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "BORROWED"


@pytest.fixture
def borrowed_checkout(records, api):
    status, response = api(payload(records))
    assert status == 201
    return UUID(response["id"]), response


def return_path(checkout_id):
    return f"/checkouts/{checkout_id}/return"


def assert_still_borrowed(database, records, checkout_id, original_notes):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.status == "BORROWED"
        assert checkout.returned_at is None and checkout.returned_to is None
        assert checkout.notes == original_notes
        assert db.get(EquipmentAsset, records["asset"]).condition_status == "WORKING"


@pytest.mark.parametrize("issuer", ["DEV-FACULTY", "DEV-ASSISTANT"])
@pytest.mark.parametrize("condition, availability", [
    ("WORKING", "AVAILABLE"), ("DAMAGED", "MAINTENANCE"), ("UNDER_REPAIR", "MAINTENANCE"),
])
def test_return_success_history_and_reborrowing(database, records, api, borrowed_checkout,
                                              issuer, condition, availability):
    checkout_id, original = borrowed_checkout
    with database.connect() as connection:
        before = connection.scalar(select(func.clock_timestamp()))
    status, response = api({"condition_status": condition, "notes": "Return inspection"},
                           issuer, path=return_path(checkout_id))
    assert status == 200
    assert response["is_overdue"] is False
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        after = db.scalar(select(func.clock_timestamp()))
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.status == response["status"] == "RETURNED"
        assert before <= checkout.returned_at <= after
        assert datetime.fromisoformat(response["returned_at"]) == checkout.returned_at
        expected_role = "FACULTY" if issuer == "DEV-FACULTY" else "LAB_ASSISTANT"
        assert checkout.returned_to == records["users"][expected_role]
        for field in ("id", "asset_id", "student_id", "issued_by"):
            assert response[field] == original[field]
        # PostgreSQL renders timestamptz in the session timezone, whereas the
        # pre-commit checkout snapshot can retain UTC or an input offset. Compare
        # exact instants (including microseconds), not their ISO string spelling.
        for field in ("checked_out_at", "due_at"):
            issued_time = datetime.fromisoformat(original[field])
            returned_time = datetime.fromisoformat(response[field])
            stored_time = getattr(checkout, field)
            assert all(value.utcoffset() is not None for value in (issued_time, returned_time, stored_time))
            assert returned_time == issued_time == stored_time
        assert checkout.notes.startswith(original["notes"] + "\n\n[RETURN] ")
        annotation = json.loads(checkout.notes.split("\n\n[RETURN] ", 1)[1])
        assert annotation["condition_status"] == condition
        assert annotation["notes"] == "Return inspection"
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.condition_status == response["condition_status"] == condition
        assert asset.availability_status == response["availability_status"] == availability

    next_status, next_response = api(payload(records))
    assert next_status == (201 if condition == "WORKING" else 409)
    with Session(database) as db:
        # The first checkout remains present after subsequent borrowing attempts.
        first = db.get(EquipmentCheckout, checkout_id)
        assert first.status == "RETURNED"
        for field in ("checked_out_at", "due_at"):
            assert getattr(first, field) == datetime.fromisoformat(original[field])
        assert json.loads(first.notes.split("\n\n[RETURN] ", 1)[1])["condition_status"] == condition
        assert db.scalar(select(func.count()).select_from(EquipmentCheckout)) == (2 if next_status == 201 else 1)


@pytest.mark.parametrize("overdue", [False, True])
def test_return_overdue_from_actual_timestamp(database, records, api, borrowed_checkout, overdue):
    checkout_id, _ = borrowed_checkout
    with Session(database) as db:
        now = db.scalar(select(func.clock_timestamp()))
        checkout = db.get(EquipmentCheckout, checkout_id)
        checkout.checked_out_at = now - timedelta(days=2)
        checkout.due_at = now + timedelta(days=-1 if overdue else 1)
        db.commit()
    status, response = api({"condition_status": "WORKING"}, path=return_path(checkout_id))
    assert status == 200
    assert response["is_overdue"] is overdue
    assert (datetime.fromisoformat(response["returned_at"]) > datetime.fromisoformat(response["due_at"])) is overdue


@pytest.mark.parametrize("issuer, expected", [
    (None, 401), ("DEV-UNKNOWN", 401), ("DEV-STUDENT", 403), ("DEV-ADMIN", 403),
    ("DEV-FACULTY", 403), ("DEV-ASSISTANT", 403),
])
def test_return_rejects_unauthorized_and_inactive_users(database, records, api, borrowed_checkout,
                                                     issuer, expected):
    checkout_id, original = borrowed_checkout
    if issuer in {"DEV-FACULTY", "DEV-ASSISTANT"}:
        role = "FACULTY" if issuer == "DEV-FACULTY" else "LAB_ASSISTANT"
        with Session(database) as db:
            db.get(User, records["users"][role]).is_active = False
            db.commit()
    assert api({"condition_status": "DAMAGED"}, issuer, path=return_path(checkout_id))[0] == expected
    assert_still_borrowed(database, records, checkout_id, original["notes"])


@pytest.mark.parametrize("availability", ["AVAILABLE", "RESERVED", "RETIRED"])
def test_return_rejects_unexpected_asset_state(database, records, api, borrowed_checkout, availability):
    checkout_id, original = borrowed_checkout
    with Session(database) as db:
        db.get(EquipmentAsset, records["asset"]).availability_status = availability
        db.commit()
    assert api({"condition_status": "WORKING"}, path=return_path(checkout_id))[0] == 409
    assert_still_borrowed(database, records, checkout_id, original["notes"])
    with Session(database) as db:
        assert db.get(EquipmentAsset, records["asset"]).availability_status == availability


def test_return_missing_checkout(database, records, api):
    assert api({"condition_status": "WORKING"}, path=return_path(uuid4()))[0] == 404
    assert_no_checkout(database, records["asset"])


@pytest.mark.parametrize("data", [
    {}, {"condition_status": "MISSING"}, {"condition_status": "MALFUNCTIONING"},
    {"condition_status": "WORKING", "notes": "x" * 1001},
    {"condition_status": "WORKING", "returned_to": str(uuid4())},
    {"condition_status": "WORKING", "returned_at": "2000-01-01T00:00:00Z"},
])
def test_return_rejects_invalid_payload(database, records, api, borrowed_checkout, data):
    checkout_id, original = borrowed_checkout
    assert api(data, path=return_path(checkout_id))[0] == 422
    assert_still_borrowed(database, records, checkout_id, original["notes"])


@pytest.mark.parametrize("reborrow", [False, True])
def test_duplicate_return_preserves_first_return_and_new_loan(database, records, api,
                                                            borrowed_checkout, reborrow):
    checkout_id, _ = borrowed_checkout
    status, original_return = api({"condition_status": "WORKING", "notes": "First return"},
                                  path=return_path(checkout_id))
    assert status == 200
    if reborrow:
        assert api(payload(records))[0] == 201
    assert api({"condition_status": "DAMAGED", "notes": "Duplicate"},
               "DEV-FACULTY", path=return_path(checkout_id))[0] == 409
    with Session(database) as db:
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.notes == original_return["notes"]
        assert checkout.returned_at == datetime.fromisoformat(original_return["returned_at"])
        assert str(checkout.returned_to) == original_return["returned_to"]
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.condition_status == "WORKING"
        assert asset.availability_status == ("BORROWED" if reborrow else "AVAILABLE")


@pytest.fixture
def history_records(database, records):
    """Two borrowers, two equipment definitions, active/late/on-time history."""
    with Session(database) as db:
        now = db.scalar(select(func.clock_timestamp()))
        other = User(id=uuid4(), college_id="OTHER-STUDENT", full_name="Private Name",
                     email="private@example.invalid", role="STUDENT", is_active=True)
        equipment = [Equipment(id=uuid4(), name=f"History sensor {i}", category="IoT",
                               tracking_type="INDIVIDUAL", default_location="Test lab") for i in range(2)]
        db.add_all([other, *equipment])
        db.flush()
        specifications = {
            "A": (records["users"]["STUDENT"], 0, -1 / 24, 1, None),
            "B": (records["users"]["STUDENT"], 0, -2, -1, None),
            "C": (records["users"]["STUDENT"], 1, -4, -3, -2),
            "D": (other.id, 1, -4, 1, -2),
            "E": (other.id, 0, -1 / 48, 1, None),
            "F": (records["users"]["STUDENT"], 1, -4, -2, -2),
        }
        loans, assets = {}, {}
        for name, (student, equipment_index, issued_days, due_days, returned_days) in specifications.items():
            asset = EquipmentAsset(id=uuid4(), equipment_id=equipment[equipment_index].id,
                                   asset_code="HISTORY-" + name, location="Test lab",
                                   availability_status="BORROWED" if returned_days is None else "AVAILABLE")
            db.add(asset)
            db.flush()
            checkout = EquipmentCheckout(
                id=uuid4(), asset_id=asset.id, student_id=student,
                issued_by=records["users"]["LAB_ASSISTANT"],
                checked_out_at=now + timedelta(days=issued_days), due_at=now + timedelta(days=due_days),
                returned_at=None if returned_days is None else now + timedelta(days=returned_days),
                returned_to=None if returned_days is None else records["users"]["FACULTY"],
                status="BORROWED" if returned_days is None else "RETURNED",
                notes="Internal inspection details",
            )
            db.add(checkout)
            loans[name], assets[name] = checkout.id, asset.id
        db.commit()
        return dict(now=now, loans=loans, assets=assets, equipment=[e.id for e in equipment],
                    own=records["users"]["STUDENT"], other=other.id)


@pytest.mark.parametrize("issuer", ["DEV-ADMIN", "DEV-FACULTY", "DEV-ASSISTANT"])
def test_history_staff_permissions_and_minimal_projection(database, api, history_records, issuer):
    data = history_records
    status, page = api(dev_user=issuer, method="GET")
    assert status == 200
    assert {item["id"] for item in page["items"]} == {str(i) for i in data["loans"].values()}
    assert page["limit"] == 25 and page["offset"] == 0 and not page["has_more"]
    assert datetime.fromisoformat(page["as_of"]).tzinfo is not None
    for item in page["items"]:
        assert not {"notes", "full_name", "email", "college_id"} & item.keys()
    assert "Private Name" not in json.dumps(page)
    assert "Internal inspection details" not in json.dumps(page)
    status, detail = api(dev_user=issuer, method="GET", path=f"/checkouts/{data['loans']['D']}")
    assert status == 200 and detail["student_id"] == str(data["other"])


def test_history_student_isolation_before_pagination(api, history_records):
    data = history_records
    own_ids = {str(data["loans"][name]) for name in "ABCF"}
    status, page = api(dev_user="DEV-STUDENT", method="GET", query={"limit": 1})
    assert status == 200 and len(page["items"]) == 1
    assert page["items"][0]["id"] == str(data["loans"]["A"])
    assert page["has_more"] is True  # Newest global record E belongs to someone else.
    status, page = api(dev_user="DEV-STUDENT", method="GET", query={"student_id": str(data["own"])})
    assert status == 200 and {item["id"] for item in page["items"]} == own_ids
    assert api(dev_user="DEV-STUDENT", method="GET", query={"student_id": str(data["other"])})[0] == 403
    for name in "DE":
        status, body = api(dev_user="DEV-STUDENT", method="GET", path=f"/checkouts/{data['loans'][name]}")
        assert status == 404 and body == {"detail": "Checkout not found"}
    assert api(dev_user="DEV-STUDENT", method="GET", path=f"/checkouts/{uuid4()}")[0] == 404
    assert api(dev_user="DEV-STUDENT", method="GET", path=f"/checkouts/{data['loans']['A']}")[0] == 200


def test_history_second_authenticated_student_is_isolated(database, records, api, history_records):
    data = history_records
    second_id = records["users"]["FACULTY"]
    # Change fixture data only: existing dev auth resolves the real persisted role.
    with Session(database) as db:
        db.get(User, second_id).role = "STUDENT"
        for name in "DE":
            db.get(EquipmentCheckout, data["loans"][name]).student_id = second_id
        db.commit()
    status, page = api(dev_user="DEV-FACULTY", method="GET")
    assert status == 200
    assert {item["id"] for item in page["items"]} == {str(data["loans"][n]) for n in "DE"}
    assert api(dev_user="DEV-FACULTY", method="GET", query={"student_id": str(data["own"])})[0] == 403
    assert api(dev_user="DEV-FACULTY", method="GET", path=f"/checkouts/{data['loans']['A']}")[0] == 404


@pytest.mark.parametrize("case", [
    "active", "returned", "overdue", "not_overdue", "returned_overdue", "equipment0", "equipment1",
    "asset", "own", "other", "from_inclusive", "before_exclusive", "combined", "unknown", "empty_page",
])
def test_history_filters_and_combinations(api, history_records, case):
    d = history_records
    cases = {
        "active": ({"status": "BORROWED"}, "ABE"),
        "returned": ({"status": "RETURNED"}, "CDF"),
        "overdue": ({"overdue": "true"}, "B"),
        "not_overdue": ({"overdue": "false"}, "ACDEF"),
        "returned_overdue": ({"status": "RETURNED", "overdue": "true"}, ""),
        "equipment0": ({"equipment_id": str(d["equipment"][0])}, "ABE"),
        "equipment1": ({"equipment_id": str(d["equipment"][1])}, "CDF"),
        "asset": ({"asset_id": str(d["assets"]["C"])}, "C"),
        "own": ({"student_id": str(d["own"])}, "ABCF"),
        "other": ({"student_id": str(d["other"])}, "DE"),
        "from_inclusive": ({"checked_out_from": (d["now"] - timedelta(days=2)).isoformat()}, "ABE"),
        "before_exclusive": ({"checked_out_before": (d["now"] - timedelta(days=2)).isoformat()}, "CDF"),
        "combined": ({"equipment_id": str(d["equipment"][1]), "student_id": str(d["own"]),
                      "status": "RETURNED", "checked_out_from": (d["now"] - timedelta(days=5)).isoformat(),
                      "checked_out_before": (d["now"] - timedelta(days=3)).isoformat()}, "CF"),
        "unknown": ({"equipment_id": str(uuid4())}, ""),
        "empty_page": ({"offset": 999}, ""),
    }
    query, expected = cases[case]
    status, response = api(method="GET", query=query)
    assert status == 200
    assert {i["id"] for i in response["items"]} == {str(d["loans"][name]) for name in expected}
    assert not response["has_more"]
    if case == "overdue":
        assert all(i["is_currently_overdue"] for i in response["items"])


def test_history_pagination_ties_and_timezone_equivalence(database, api, history_records):
    d = history_records
    with Session(database) as db:
        expected = [str(i) for i in db.scalars(select(EquipmentCheckout.id).order_by(
            EquipmentCheckout.checked_out_at.desc(), EquipmentCheckout.id.desc()
        )).all()]
    actual = []
    for offset in (0, 2, 4):
        status, page = api(method="GET", query={"limit": 2, "offset": offset})
        assert status == 200 and page["has_more"] is (offset < 4)
        actual.extend(item["id"] for item in page["items"])
    assert actual == expected and len(set(actual)) == 6
    cutoff = d["now"] - timedelta(days=2)
    queries = [{"checked_out_from": cutoff.isoformat()},
               {"checked_out_from": cutoff.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()}]
    pages = [api(method="GET", query=q)[1] for q in queries]
    assert [i["id"] for i in pages[0]["items"]] == [i["id"] for i in pages[1]["items"]]


@pytest.mark.parametrize("query", [
    {"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": str(2**63)}, {"status": "OVERDUE"}, {"overdue": "maybe"},
    {"student_id": "invalid"}, {"equipment_id": "invalid"}, {"asset_id": "invalid"},
    {"checked_out_from": "2026-11-02T12:00:00"}, {"checked_out_before": "invalid"},
    {"checked_out_from": "2026-11-03T00:00:00Z", "checked_out_before": "2026-11-02T00:00:00Z"},
    {"unknown": "value"},
])
def test_history_invalid_query_parameters(api, records, query):
    assert api(method="GET", query=query)[0] == 422


@pytest.mark.parametrize("issuer, inactive", [(None, False), ("DEV-UNKNOWN", False),
    ("DEV-STUDENT", True), ("DEV-ADMIN", True), ("DEV-FACULTY", True), ("DEV-ASSISTANT", True)])
def test_history_auth_rejections_on_both_endpoints(database, api, records, history_records, issuer, inactive):
    if inactive:
        with Session(database) as db:
            user = db.scalar(select(User).where(User.college_id == issuer))
            user.is_active = False
            db.commit()
    expected = 403 if inactive else 401
    assert api(dev_user=issuer, method="GET")[0] == expected
    assert api(dev_user=issuer, method="GET", path=f"/checkouts/{history_records['loans']['A']}")[0] == expected


def test_history_is_read_only_and_uses_one_clock_per_request(database, api, history_records):
    def snapshot():
        with database.connect() as connection:
            return (
                connection.execute(select(EquipmentCheckout.__table__).order_by(EquipmentCheckout.id)).all(),
                connection.execute(select(EquipmentAsset.__table__).order_by(EquipmentAsset.id)).all(),
            )

    before = snapshot()
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(database, "before_cursor_execute", record)
    try:
        # The API fixture also enforces SET TRANSACTION READ ONLY for these GETs.
        assert api(method="GET")[0] == 200
        assert api(method="GET", path=f"/checkouts/{history_records['loans']['B']}")[0] == 200
    finally:
        event.remove(database, "before_cursor_execute", record)
    assert snapshot() == before
    assert sum("clock_timestamp()" in sql for sql in statements) == 2
    assert all(sql.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY")) for sql in statements)
    assert all("FOR UPDATE" not in sql.upper() for sql in statements)


def test_history_exact_deadline_boundaries_with_database_generated_clock(database, records, history_records):
    from unittest.mock import patch

    d = history_records
    with Session(database) as db:
        # Obtain the fixed evaluation instant from PostgreSQL, not the test host.
        as_of = db.scalar(select(func.clock_timestamp()))
        for name, delta in (("A", 0), ("B", -1), ("E", 1)):
            db.get(EquipmentCheckout, d["loans"][name]).due_at = as_of + timedelta(microseconds=delta)
        for name, delta in (("C", 1), ("D", 0), ("F", -1)):
            checkout = db.get(EquipmentCheckout, d["loans"][name])
            checkout.returned_at = checkout.due_at + timedelta(microseconds=delta)
        db.commit()
    with Session(database, autoflush=False) as db:
        staff = db.get(User, records["users"]["FACULTY"])
        # Freeze only the clock lookup; all history calculations/filters run in SQL.
        with patch.object(db, "scalar", return_value=as_of) as clock:
            result = list_borrowing_history(db, staff, HistoryFilters())
        clock.assert_called_once()
        by_id = {item.id: item for item in result.items}
        assert not by_id[d["loans"]["A"]].is_currently_overdue
        assert by_id[d["loans"]["B"]].is_currently_overdue
        assert not by_id[d["loans"]["E"]].is_currently_overdue
        assert by_id[d["loans"]["C"]].was_returned_late
        assert not by_id[d["loans"]["D"]].was_returned_late
        assert not by_id[d["loans"]["F"]].was_returned_late
        with patch.object(db, "scalar", return_value=as_of):
            page = list_borrowing_history(db, staff, HistoryFilters(overdue=True))
        assert [item.id for item in page.items] == [d["loans"]["B"]]


def test_history_dynamic_overdue_and_historical_lateness(database, api, history_records):
    d = history_records
    status, detail = api(method="GET", path=f"/checkouts/{d['loans']['C']}")
    assert status == 200 and detail["was_returned_late"] and not detail["is_currently_overdue"]
    # Change only a test record's deadline; the next read must recompute overdue.
    with Session(database) as db:
        db.get(EquipmentCheckout, d["loans"]["A"]).due_at = db.scalar(select(func.clock_timestamp())) - timedelta(minutes=1)
        db.commit()
    status, detail = api(method="GET", path=f"/checkouts/{d['loans']['A']}")
    assert status == 200 and detail["is_currently_overdue"] and not detail["was_returned_late"]


def test_history_maximum_page_size(database, api, records, history_records):
    d = history_records
    with Session(database) as db:
        for _ in range(101):
            db.add(EquipmentCheckout(
                asset_id=d["assets"]["A"], student_id=d["own"], issued_by=records["users"]["FACULTY"],
                checked_out_at=d["now"] - timedelta(days=5), due_at=d["now"] - timedelta(days=4),
                returned_at=d["now"] - timedelta(days=4), returned_to=records["users"]["FACULTY"], status="RETURNED",
            ))
        db.commit()
    status, page = api(method="GET", query={"limit": 100})
    assert status == 200 and len(page["items"]) == 100 and page["has_more"] is True
    status, page = api(method="GET", query={"limit": 100, "offset": 100})
    assert status == 200 and len(page["items"]) == 7 and page["has_more"] is False


def test_fabricated_receiver_rejected_without_writes(database, records, borrowed_checkout):
    checkout_id, original = borrowed_checkout
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        fabricated_receiver = User(id=uuid4(), role="FACULTY", is_active=True)
        with pytest.raises(HTTPException) as error:
            record_return(db, checkout_id, fabricated_receiver,
                          ReturnCreate(condition_status="DAMAGED", notes="Must roll back"))
        assert error.value.status_code == 403
        assert db.get(EquipmentCheckout, checkout_id).status == "BORROWED"
    assert_still_borrowed(database, records, checkout_id, original["notes"])
    with Session(database) as db:
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "BORROWED"


@pytest.mark.parametrize("race_with_checkout", [False, True])
def test_return_concurrency_and_database_clock_after_lock_wait(database, records,
                                                             borrowed_checkout, race_with_checkout):
    checkout_id, original = borrowed_checkout
    label = "return_race_" + uuid4().hex

    def attempt(is_checkout):
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            label_transaction(db, label)
            staff = User(id=records["users"]["FACULTY"])
            try:
                if is_checkout:
                    result = create_checkout(db, records["asset"], records["users"]["STUDENT"],
                                             staff, datetime.now(timezone.utc) + timedelta(days=1))
                    return 201, result.id
                result = record_return(db, checkout_id, staff, ReturnCreate(condition_status="WORKING"))
                return 200, result.returned_at
            except HTTPException as error:
                return error.status_code, None

    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        transaction = blocker.begin()
        blocker.execute(select(EquipmentAsset).where(EquipmentAsset.id == records["asset"]).with_for_update())
        futures = [pool.submit(attempt, False), pool.submit(attempt, race_with_checkout)]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = :label AND wait_event_type = 'Lock'"
                    ), {"label": label})
                if waiting == 2:
                    break
                time.sleep(0.05)
            assert waiting == 2, "Both requests must wait on the same asset row lock"
            released_at = blocker.scalar(select(func.clock_timestamp()))
        finally:
            transaction.rollback()
        results = [future.result(timeout=15) for future in futures]

    if race_with_checkout:
        assert results[0][0] == 200
        assert results[1][0] in {201, 409}
    else:
        assert sorted(status for status, _ in results) == [200, 409]
    returned_at = next(value for status, value in results if status == 200)
    assert returned_at >= released_at  # now() would incorrectly record transaction start.
    with Session(database) as db:
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.status == "RETURNED"
        assert checkout.returned_at == returned_at
        assert checkout.notes.startswith(original["notes"])
        assert checkout.notes.count("\n\n[RETURN] ") == 1
        borrowed = db.scalars(select(EquipmentCheckout).where(EquipmentCheckout.status == "BORROWED")).all()
        reborrowed = race_with_checkout and results[1][0] == 201
        assert len(borrowed) == (1 if reborrowed else 0)
        if reborrowed:
            assert borrowed[0].id == results[1][1]
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.availability_status == ("BORROWED" if reborrowed else "AVAILABLE")
        assert asset.condition_status == "WORKING"
