"""PostgreSQL integration tests for the maintenance API."""

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import User
from backend.app.routers.maintenance import router
from backend.app.models.equipment import Equipment, EquipmentAsset


@pytest.fixture
def api(database, monkeypatch):
    """Create an isolated maintenance API test client."""

    monkeypatch.setattr(
        "backend.app.auth.dependencies.is_dev_auth_enabled",
        lambda: True,
    )

    app = FastAPI()
    app.include_router(router)

    def read_sessions(request: Request):
        with Session(
            database,
            autoflush=False,
            expire_on_commit=False,
        ) as db:
            if request.method == "GET":
                db.execute(text("SET TRANSACTION READ ONLY"))

            yield db

    def write_sessions():
        with Session(
            database,
            autoflush=False,
            expire_on_commit=False,
        ) as db:
            yield db

    app.dependency_overrides[get_db] = read_sessions
    app.dependency_overrides[get_inventory_write_db] = write_sessions

    with TestClient(
        app,
        client=("127.0.0.1", 12345),
    ) as client:
        yield client


@pytest.fixture
def users(database):
    """Create development users with different roles."""

    identities = (
        ("ADMIN", "DEV-ADMIN"),
        ("FACULTY", "DEV-FACULTY"),
        ("LAB_ASSISTANT", "DEV-ASSISTANT"),
        ("STUDENT", "DEV-STUDENT"),
    )

    with Session(database, expire_on_commit=False) as db:
        for role, college_id in identities:
            db.add(
                User(
                    id=uuid4(),
                    college_id=college_id,
                    full_name="Maintenance integration test",
                    email=f"{college_id}@example.invalid",
                    role=role,
                    is_active=True,
                )
            )

        db.commit()


def test_student_cannot_view_maintenance(api, users):
    """Students must not access maintenance records."""

    response = api.get(
        f"/maintenance/{uuid4()}",
        headers={"X-Dev-User": "DEV-STUDENT"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "You do not have permission to perform this action"
    )

@pytest.mark.parametrize(
    "dev_user,expected_status",
    [
        ("DEV-ADMIN", 404),
        ("DEV-FACULTY", 404),
        ("DEV-ASSISTANT", 404),
        ("DEV-STUDENT", 403),
    ],
)
def test_maintenance_read_permissions(
    api,
    users,
    dev_user,
    expected_status,
):
    """Verify maintenance read permissions for each role."""

    response = api.get(
        f"/maintenance/{uuid4()}",
        headers={"X-Dev-User": dev_user},
    )

    assert response.status_code == expected_status

def test_complete_maintenance_workflow(api, users, database):
    """Test a complete equipment maintenance lifecycle."""

    # Create equipment and an individual asset.
    with Session(database, expire_on_commit=False) as db:
        equipment = Equipment(
            id=uuid4(),
            name="Maintenance Test Sensor",
            category="IoT",
            tracking_type="INDIVIDUAL",
            default_location="IoT Lab",
        )

        db.add(equipment)
        db.flush()

        asset = EquipmentAsset(
            id=uuid4(),
            equipment_id=equipment.id,
            asset_code=f"MAINT-{uuid4().hex[:12]}",
            location="IoT Lab",
        )

        db.add(asset)
        db.commit()

        asset_id = asset.id

    def post(path, payload, user):
        return api.post(
            path,
            json=payload,
            headers={"X-Dev-User": user},
        )

    # 1. Report equipment damage.
    response = post(
        "/maintenance",
        {
            "asset_id": str(asset_id),
            "issue_type": "DAMAGE",
            "issue_description": "Sensor is not functioning.",
        },
        "DEV-FACULTY",
    )

    assert response.status_code == 201, response.text

    maintenance = response.json()
    request_id = maintenance["id"]

    assert maintenance["status"] == "OPEN"
    assert maintenance["maintenance_hold"] is True

    # 2. Assign the repair to a lab assistant.
    with Session(database) as db:
        assistant = db.query(User).filter(
            User.college_id == "DEV-ASSISTANT"
        ).one()

        assistant_id = assistant.id

    response = post(
        f"/maintenance/{request_id}/assign",
        {
            "assignee_id": str(assistant_id),
            "expected_version": maintenance["version"],
        },
        "DEV-FACULTY",
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    # 3. Start the repair.
    response = post(
        f"/maintenance/{request_id}/start",
        {"expected_version": maintenance["version"]},
        "DEV-ASSISTANT",
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    assert maintenance["status"] == "IN_PROGRESS"

    # 4. Submit the completed repair.
    response = post(
        f"/maintenance/{request_id}/submit",
        {
            "expected_version": maintenance["version"],
            "repair_notes": "Replaced faulty sensor component.",
        },
        "DEV-ASSISTANT",
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    assert maintenance["status"] == "AWAITING_VERIFICATION"

    # 5. Independently verify the repair.
    response = post(
        f"/maintenance/{request_id}/verify",
        {
            "expected_version": maintenance["version"],
            "outcome": "PASS",
            "notes": "Sensor tested successfully.",
        },
        "DEV-FACULTY",
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    assert maintenance["maintenance_hold"] is False
    assert maintenance["availability_status"] == "AVAILABLE"
    assert maintenance["condition_status"] == "WORKING"

    # 6. Retrieve maintenance history.
    response = api.get(
        f"/maintenance/{request_id}/events",
        headers={"X-Dev-User": "DEV-FACULTY"},
    )

    assert response.status_code == 200, response.text

    history = response.json()

    assert len(history["items"]) >= 5

    # 7. Verify the asset's final database state.
    with Session(database) as db:
        saved_asset = db.get(EquipmentAsset, asset_id)

        assert saved_asset.availability_status == "AVAILABLE"
        assert saved_asset.condition_status == "WORKING"
        assert saved_asset.maintenance_hold is False

@pytest.mark.parametrize(
    "path,payload",
    [
        (
            "/maintenance",
            {
                "asset_id": str(uuid4()),
                "issue_type": "DAMAGE",
                "issue_description": "Unauthorized maintenance report",
            },
        ),
        (
            f"/maintenance/{uuid4()}/recover",
            {"issue_description": "Unauthorized recovery"},
        ),
        (
            f"/maintenance/{uuid4()}/assign",
            {
                "assignee_id": str(uuid4()),
                "expected_version": 1,
            },
        ),
        (
            f"/maintenance/{uuid4()}/start",
            {"expected_version": 1},
        ),
        (
            f"/maintenance/{uuid4()}/submit",
            {
                "expected_version": 1,
                "repair_notes": "Unauthorized repair",
            },
        ),
        (
            f"/maintenance/{uuid4()}/verify",
            {
                "expected_version": 1,
                "outcome": "PASS",
                "notes": "Unauthorized verification",
            },
        ),
        (
            f"/maintenance/{uuid4()}/cancel",
            {
                "expected_version": 1,
                "reason": "Unauthorized cancellation",
            },
        ),
    ],
)
def test_student_cannot_modify_maintenance(api, users, path, payload):
    """Students must not be able to perform maintenance operations."""

    response = api.post(
        path,
        json=payload,
        headers={"X-Dev-User": "DEV-STUDENT"},
    )

    assert response.status_code == 403, response.text

@pytest.mark.parametrize(
    "dev_user,operation,payload",
    [
        (
            "DEV-ASSISTANT",
            "assign",
            {
                "assignee_id": str(uuid4()),
                "expected_version": 1,
            },
        ),
        (
            "DEV-ASSISTANT",
            "verify",
            {
                "expected_version": 1,
                "outcome": "PASS",
                "notes": "Unauthorized verification",
            },
        ),
        (
            "DEV-ASSISTANT",
            "cancel",
            {
                "expected_version": 1,
                "reason": "Unauthorized cancellation",
            },
        ),
        (
            "DEV-ADMIN",
            "start",
            {"expected_version": 1},
        ),
        (
            "DEV-ADMIN",
            "submit",
            {
                "expected_version": 1,
                "repair_notes": "Unauthorized repair",
            },
        ),
    ],
)
def test_staff_maintenance_write_permissions(
    api, users, dev_user, operation, payload
):
    """Verify that staff cannot exceed their maintenance permissions."""

    response = api.post(
        f"/maintenance/{uuid4()}/{operation}",
        json=payload,
        headers={"X-Dev-User": dev_user},
    )

    assert response.status_code == 403, response.text

def test_maintenance_rejects_stale_version(api, users, database):
    """Outdated maintenance updates must not overwrite newer changes."""

    # Create test equipment and asset.
    with Session(database, expire_on_commit=False) as db:
        equipment = Equipment(
            id=uuid4(),
            name="Concurrency Test Sensor",
            category="IoT",
            tracking_type="INDIVIDUAL",
            default_location="IoT Lab",
        )

        db.add(equipment)
        db.flush()

        asset = EquipmentAsset(
            id=uuid4(),
            equipment_id=equipment.id,
            asset_code=f"CONC-{uuid4().hex[:12]}",
            location="IoT Lab",
        )

        db.add(asset)
        db.commit()
        asset_id = asset.id

    headers = {"X-Dev-User": "DEV-FACULTY"}

    # Report maintenance issue.
    response = api.post(
        "/maintenance",
        json={
            "asset_id": str(asset_id),
            "issue_type": "MALFUNCTION",
            "issue_description": "Sensor readings are inconsistent.",
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text

    maintenance = response.json()
    request_id = maintenance["id"]
    original_version = maintenance["version"]

    # Find the lab assistant.
    with Session(database) as db:
        assistant = db.query(User).filter(
            User.college_id == "DEV-ASSISTANT"
        ).one()

        assistant_id = assistant.id

    # First update succeeds.
    response = api.post(
        f"/maintenance/{request_id}/assign",
        json={
            "assignee_id": str(assistant_id),
            "expected_version": original_version,
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text

    updated_maintenance = response.json()

    assert updated_maintenance["version"] > original_version

    # Second update attempts to use the outdated version.
    response = api.post(
        f"/maintenance/{request_id}/assign",
        json={
            "assignee_id": str(assistant_id),
            "expected_version": original_version,
        },
        headers=headers,
    )

    assert response.status_code == 409, response.text

    # Confirm the failed update did not change the record.
    response = api.get(
        f"/maintenance/{request_id}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["version"] == updated_maintenance["version"]

def test_failed_verification_keeps_asset_unavailable(api, users, database):
    """Failed repair verification must not release equipment."""

    # Create test equipment and asset.
    with Session(database, expire_on_commit=False) as db:
        equipment = Equipment(
            id=uuid4(),
            name="Failed Verification Test Sensor",
            category="IoT",
            tracking_type="INDIVIDUAL",
            default_location="IoT Lab",
        )

        db.add(equipment)
        db.flush()

        asset = EquipmentAsset(
            id=uuid4(),
            equipment_id=equipment.id,
            asset_code=f"FAIL-{uuid4().hex[:12]}",
            location="IoT Lab",
        )

        db.add(asset)
        db.commit()
        asset_id = asset.id

    faculty_headers = {"X-Dev-User": "DEV-FACULTY"}
    assistant_headers = {"X-Dev-User": "DEV-ASSISTANT"}

    # 1. Report equipment malfunction.
    response = api.post(
        "/maintenance",
        json={
            "asset_id": str(asset_id),
            "issue_type": "MALFUNCTION",
            "issue_description": "Sensor produces incorrect readings.",
        },
        headers=faculty_headers,
    )

    assert response.status_code == 201, response.text

    maintenance = response.json()
    request_id = maintenance["id"]

    # 2. Find the lab assistant.
    with Session(database) as db:
        assistant = db.query(User).filter(
            User.college_id == "DEV-ASSISTANT"
        ).one()

        assistant_id = assistant.id

    # 3. Assign repairer.
    response = api.post(
        f"/maintenance/{request_id}/assign",
        json={
            "assignee_id": str(assistant_id),
            "expected_version": maintenance["version"],
        },
        headers=faculty_headers,
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    # 4. Start repair.
    response = api.post(
        f"/maintenance/{request_id}/start",
        json={"expected_version": maintenance["version"]},
        headers=assistant_headers,
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    # 5. Submit repair.
    response = api.post(
        f"/maintenance/{request_id}/submit",
        json={
            "expected_version": maintenance["version"],
            "repair_notes": "Recalibrated the sensor.",
        },
        headers=assistant_headers,
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    assert maintenance["status"] == "AWAITING_VERIFICATION"

    # 6. Faculty rejects the repair.
    response = api.post(
        f"/maintenance/{request_id}/verify",
        json={
            "expected_version": maintenance["version"],
            "outcome": "FAIL",
            "notes": "Sensor readings are still incorrect.",
        },
        headers=faculty_headers,
    )

    assert response.status_code == 200, response.text

    maintenance = response.json()

    assert maintenance["status"] == "IN_PROGRESS"
    assert maintenance["maintenance_hold"] is True
    assert maintenance["condition_status"] == "UNDER_REPAIR"
    assert maintenance["availability_status"] != "AVAILABLE"

    # 7. Confirm the actual database state.
    with Session(database) as db:
        saved_asset = db.get(EquipmentAsset, asset_id)

        assert saved_asset is not None
        assert saved_asset.maintenance_hold is True
        assert saved_asset.condition_status == "UNDER_REPAIR"
        assert saved_asset.availability_status != "AVAILABLE"

    # 8. Confirm the failure was recorded in maintenance history.
    response = api.get(
        f"/maintenance/{request_id}/events",
        headers=faculty_headers,
    )

    assert response.status_code == 200, response.text

    events = response.json()["items"]

    assert any(
        event["event_type"] == "VERIFICATION_FAILED"
        for event in events
    )

def test_repairer_cannot_verify_own_repair(api, users, database):
    """A faculty member must not verify a repair they completed."""

    # Create test equipment.
    with Session(database, expire_on_commit=False) as db:
        equipment = Equipment(
            id=uuid4(),
            name="Independent Verification Test Sensor",
            category="IoT",
            tracking_type="INDIVIDUAL",
            default_location="IoT Lab",
        )

        db.add(equipment)
        db.flush()

        asset = EquipmentAsset(
            id=uuid4(),
            equipment_id=equipment.id,
            asset_code=f"VERIFY-{uuid4().hex[:12]}",
            location="IoT Lab",
        )

        db.add(asset)
        db.commit()
        asset_id = asset.id

    headers = {"X-Dev-User": "DEV-FACULTY"}

    # 1. Report a malfunction.
    response = api.post(
        "/maintenance",
        json={
            "asset_id": str(asset_id),
            "issue_type": "MALFUNCTION",
            "issue_description": "Sensor is malfunctioning.",
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text
    maintenance = response.json()
    request_id = maintenance["id"]

    # 2. Assign the faculty member to perform the repair.
    with Session(database) as db:
        faculty = db.query(User).filter(
            User.college_id == "DEV-FACULTY"
        ).one()

        faculty_id = faculty.id

    response = api.post(
        f"/maintenance/{request_id}/assign",
        json={
            "assignee_id": str(faculty_id),
            "expected_version": maintenance["version"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    # 3. Start the repair.
    response = api.post(
        f"/maintenance/{request_id}/start",
        json={"expected_version": maintenance["version"]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    # 4. Submit the repair.
    response = api.post(
        f"/maintenance/{request_id}/submit",
        json={
            "expected_version": maintenance["version"],
            "repair_notes": "Replaced the faulty component.",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    maintenance = response.json()

    assert maintenance["status"] == "AWAITING_VERIFICATION"

    # 5. Attempt to verify the repair using the same faculty account.
    response = api.post(
        f"/maintenance/{request_id}/verify",
        json={
            "expected_version": maintenance["version"],
            "outcome": "PASS",
            "notes": "Self-verification attempt.",
        },
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == (
        "Independent verification is required"
    )

    # 6. Confirm the rejected verification did not release the asset.
    with Session(database) as db:
        saved_asset = db.get(EquipmentAsset, asset_id)

        assert saved_asset is not None
        assert saved_asset.maintenance_hold is True
        assert saved_asset.availability_status != "AVAILABLE"

    # 7. Confirm the maintenance request is still awaiting verification.
    response = api.get(
        f"/maintenance/{request_id}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "AWAITING_VERIFICATION"
    assert response.json()["version"] == maintenance["version"]