"""Shared registration payloads and ASGI transport; no database configuration."""
import asyncio
import json
from uuid import uuid4

from fastapi import FastAPI
from backend.app.database import get_db, get_inventory_write_db
from backend.app.routers.equipment import router as equipment_router
from backend.app.routers.asset import router as asset_router


def equipment_payload(**changes):
    return {"name": "Network sensor", "category": "IoT", "tracking_type": "INDIVIDUAL",
            "default_location": "Networks lab", **changes}


def asset_payload(equipment_id=None, **changes):
    return {"equipment_id": str(uuid4() if equipment_id is None else equipment_id), "asset_code": "REG-ASSET", "location": "Networks lab", **changes}


def registration_app(sessions, write_sessions):
    app = FastAPI()
    app.include_router(equipment_router)
    app.include_router(asset_router)
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_inventory_write_db] = write_sessions
    return app


def post_json(app, path, payload, header="DEV-FACULTY", client="127.0.0.1"):
    async def run():
        headers = [(b"content-type", b"application/json")]
        if header is not None:
            headers.append((b"x-dev-user", header.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
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
