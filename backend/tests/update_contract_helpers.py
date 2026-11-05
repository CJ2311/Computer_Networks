"""Future update contracts and explicit readiness gates; no application stand-ins."""
import asyncio
import importlib
import importlib.util
import json

import pytest

from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.checkout import EquipmentCheckout


CONTRACTS = {
    "equipment": dict(model=Equipment, path="/equipment", schema="EquipmentUpdate", service="update_equipment",
                      editable={"name", "category", "manufacturer", "model_number", "description", "default_location", "purchase_price"},
                      nullable={"manufacturer", "model_number", "description", "purchase_price"},
                      required={"name", "category", "default_location"},
                      immutable={"id", "tracking_type", "created_at"},
                      lengths={"name": 150, "category": 100, "manufacturer": 100, "model_number": 100, "default_location": 150}),
    "asset": dict(model=EquipmentAsset, path="/assets", schema="AssetUpdate", service="update_asset",
                  editable={"asset_code", "nfc_tag_id", "serial_number", "location"},
                  nullable={"nfc_tag_id", "serial_number"}, required={"asset_code", "location"},
                  immutable={"id", "equipment_id", "availability_status", "condition_status", "maintenance_hold", "created_at"},
                  lengths={"asset_code": 50, "location": 150, "nfc_tag_id": 100, "serial_number": 100}),
}
AUDIT_MODULE = "backend.app.models.metadata_audit"
AUDIT_MODEL = "MetadataAuditEvent"
AUDIT_PARENT_REVISION = "b7d2f90c4e61"
CHECKOUT_SNAPSHOT_FIELDS = ("equipment_name_at_checkout", "asset_code_at_checkout", "identifier_snapshot_status")
AUDIT_ACTOR_SETTING = "inventory.metadata_actor_id"
AUDIT_WRITER_ROLE = "inventory_metadata_audit_writer"
AUDIT_OWNER_ROLE = "inventory_metadata_audit_owner"
AUDIT_GENERATORS = ("metadata_audit_equipment_change", "metadata_audit_asset_change")


def checkout_snapshots_ready():
    missing = [name for name in CHECKOUT_SNAPSHOT_FIELDS if name not in EquipmentCheckout.__table__.c]
    if missing:
        pytest.skip("Pending Option A checkout snapshots: " + ", ".join(missing))



def schema_for(kind):
    module = importlib.import_module(f"backend.app.schemas.{kind}")
    schema = getattr(module, CONTRACTS[kind]["schema"], None)
    if schema is None:
        pytest.skip(f"Pending Task 3B: {CONTRACTS[kind]['schema']} is not implemented")
    return schema


def missing_update_components():
    missing = []
    for kind, contract in CONTRACTS.items():
        if not hasattr(importlib.import_module(f"backend.app.schemas.{kind}"), contract["schema"]):
            missing.append(contract["schema"])
        if not hasattr(importlib.import_module(f"backend.app.services.{kind}"), contract["service"]):
            missing.append(contract["service"])
    if importlib.util.find_spec(AUDIT_MODULE) is None:
        missing.append(AUDIT_MODEL)
    return missing


def runtime_for(kind):
    schema = schema_for(kind)
    module = importlib.import_module(f"backend.app.services.{kind}")
    operation = getattr(module, CONTRACTS[kind]["service"], None)
    if operation is None:
        pytest.skip(f"Pending Task 3B: {CONTRACTS[kind]['service']} is not implemented")
    if importlib.util.find_spec(AUDIT_MODULE) is None:
        pytest.fail("Update service exists without the required metadata audit model/migration")
    audit = getattr(importlib.import_module(AUDIT_MODULE), AUDIT_MODEL, None)
    assert audit is not None, "Required MetadataAuditEvent model is missing"
    return schema, operation, audit


def patch_json(app, path, payload, header="DEV-FACULTY", client="127.0.0.1"):
    async def run():
        headers = [(b"content-type", b"application/json")]
        if header is not None:
            headers.append((b"x-dev-user", header.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "PATCH", "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "headers": headers,
                 "client": (client, 12345), "server": ("127.0.0.1", 80)}
        messages = []
        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        async def send(message):
            messages.append(message)
        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        return status, body
    return asyncio.run(run())
