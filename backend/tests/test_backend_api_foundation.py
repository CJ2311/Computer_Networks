"""ASGI requests with mocked Sessions; application engine is forbidden."""
import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.user import User
from backend.app.routers.asset import router as asset_router
from backend.app.routers.equipment import router as equipment_router
from backend.app.routers.auth import router as auth_router
from backend.app.routers.maintenance import router as maintenance_router


def call(app,path,query=b"",method="GET",payload=None):
    async def run():
        messages=[]
        sent=False
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {"type":"http.request","body":json.dumps(payload).encode() if payload else b"","more_body":False}
            await asyncio.Event().wait()
        async def send(message):
            messages.append(message)
        scope={"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":method,
               "scheme":"http","path":path,"raw_path":path.encode(),"query_string":query,
               "headers":[(b"content-type",b"application/json")],"root_path":"",
               "client":("127.0.0.1",1234),"server":("127.0.0.1",80)}
        await asyncio.wait_for(app(scope,receive,send),timeout=10)
        return next(m["status"] for m in messages if m["type"]=="http.response.start"),json.loads(b"".join(m.get("body",b"") for m in messages if m["type"]=="http.response.body"))
    return asyncio.run(run())


@pytest.fixture
def api():
    app=FastAPI()
    for router in (asset_router,equipment_router,auth_router,maintenance_router):
        app.include_router(router)
    db=Mock(no_autoflush=nullcontext())
    user=User(id=uuid4(),college_id="U",full_name="Person",role="FACULTY",is_active=True)
    def sessions():
        yield db
    app.dependency_overrides[get_db]=sessions
    app.dependency_overrides[get_inventory_write_db]=sessions
    app.dependency_overrides[get_current_user]=lambda:user
    return app,db,user


def test_me_returns_safe_public_identity(api):
    app,db,user=api
    status,body=call(app,"/auth/me")
    assert status==200 and body["id"]==str(user.id)
    assert set(body)=={"id","college_id","full_name","role","is_active"}


@pytest.mark.parametrize("path",["/equipment","/equipment/"+str(uuid4())])
def test_equipment_reads_require_authentication(api,monkeypatch,path):
    app,db,user=api
    app.dependency_overrides.pop(get_current_user)
    monkeypatch.delenv("AUTH_MODE",raising=False)
    monkeypatch.setenv("DEV_AUTH_ENABLED","false")
    assert call(app,path)[0]==403
    db.scalars.assert_not_called();db.get.assert_not_called()


@pytest.mark.parametrize("path",["/equipment","/assets"])
def test_list_pagination_is_bounded_and_reaches_query(api,path):
    app,db,user=api
    db.scalars.return_value.all.return_value=[]
    assert call(app,path,b"limit=2&offset=3")== (200,[])
    stmt=db.scalars.call_args.args[0]
    assert stmt._limit_clause.value==2 and stmt._offset_clause.value==3
    assert call(app,path,b"limit=101")[0]==422
    assert call(app,path,b"offset=-1")[0]==422


def test_nfc_lookup_returns_asset_and_rejects_student(api):
    app,db,user=api
    asset=EquipmentAsset(id=uuid4(),equipment_id=uuid4(),asset_code="A",nfc_tag_id="tag",location="Lab",availability_status="AVAILABLE",condition_status="WORKING",maintenance_hold=False)
    db.scalar.return_value=asset
    assert call(app,"/assets/lookup",b"nfc_tag_id=tag")[0]==200
    user.role="STUDENT"
    assert call(app,"/assets/lookup",b"nfc_tag_id=tag")[0]==403


def test_student_cannot_adjust_stock_or_add_notes(api):
    app,db,user=api
    user.role="STUDENT"
    assert call(app,"/equipment/"+str(uuid4())+"/stock/adjustments",method="POST",payload={"expected_quantity":0,"change":1})[0]==403
    assert call(app,"/maintenance/"+str(uuid4())+"/notes",method="POST",payload={"expected_version":1,"note":"note"})[0]==403
    db.commit.assert_not_called()


def test_all_business_routes_have_auth_dependency():
    from backend.app.main import app
    def guarded(dependant):
        return dependant.call is get_current_user or any(guarded(child) for child in dependant.dependencies)
    for route in app.routes:
        if hasattr(route,"dependant") and route.path not in {"/","/health"}:
            assert guarded(route.dependant),route.path
    schema=app.openapi()
    assert "/assets/lookup" in schema["paths"]
    assert "/maintenance" in schema["paths"]
    assert "/equipment/{equipment_id}/stock/adjustments" in schema["paths"]


@pytest.mark.parametrize("path,method,payload",[
    ("/students/lookup?college_id=S","GET",None),
    ("/equipment/"+str(uuid4())+"/ledger","GET",None),
    ("/equipment/"+str(uuid4())+"/loan-policy","POST",{"allow_offsite":True}),
    ("/quantity-loans","POST",{"idempotency_key":str(uuid4()),"equipment_id":str(uuid4()),"student_id":str(uuid4()),"quantity":1,"due_at":"2030-01-01T00:00:00Z"}),
    ("/assets/bulk","POST",{"items":[{"equipment_id":str(uuid4()),"asset_code":"A","location":"Lab"}]}),
    ("/equipment/bulk","POST",{"items":[{"name":"A","category":"Lab","tracking_type":"INDIVIDUAL","default_location":"Lab"}]}),
])
def test_students_cannot_invoke_new_staff_operations(api,path,method,payload):
    from backend.app.routers.operations import router
    app,db,user=api;app.include_router(router);user.role="STUDENT"
    path,_,query=path.partition("?")
    assert call(app,path,query.encode(),method,payload)[0]==403
    db.commit.assert_not_called()
