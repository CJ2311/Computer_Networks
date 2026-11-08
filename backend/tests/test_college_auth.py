"""College identity boundary tests: no external network requests."""
import json
import time
from contextlib import nullcontext
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from backend.app.auth import college
from backend.app.auth.dependencies import get_current_user
from backend.app.models.user import User


@pytest.fixture
def provider(monkeypatch):
    values=dict(AUTH_MODE="college_introspection",COLLEGE_INTROSPECTION_URL="https://identity.example.invalid/introspection",
                COLLEGE_ISSUER="https://identity.example.invalid",COLLEGE_AUDIENCE="inventory-api",COLLEGE_CLIENT_ID="inventory-client",COLLEGE_CLIENT_SECRET="synthetic-secret")
    for key,value in values.items():
        monkeypatch.setenv(key,value)
    response=Mock(status=200)
    claims=dict(active=True,iss=values["COLLEGE_ISSUER"],aud=["inventory-api"],exp=time.time()+300,sub="subject-1",token_type="Bearer")
    response.read.side_effect=lambda length:json.dumps(claims).encode()
    opener=Mock()
    opener.open.return_value=nullcontext(response)
    monkeypatch.setattr(college,"build_opener",lambda *handlers:opener)
    return values,claims,opener,response


def test_introspection_pins_endpoint_issuer_audience_and_tls(provider):
    values,claims,opener,response=provider
    assert college.introspect("Bearer synthetic.access.token")== (values["COLLEGE_ISSUER"],"subject-1")
    request=opener.open.call_args.args[0]
    assert request.full_url==values["COLLEGE_INTROSPECTION_URL"]
    assert request.method=="POST" and b"token=synthetic.access.token" in request.data
    assert opener.open.call_args.kwargs["timeout"]==5
    response.read.assert_called_once_with(16385)


@pytest.mark.parametrize("key,value",[("active",False),("active",1),("iss","https://other.invalid"),("aud",["other"]),("exp",0),
    ("exp",True),("exp",float("nan")),("exp",2**4096),("nbf",time.time()+10000),("sub",""),("sub","bad\x00"),("token_type","id_token")])
def test_invalid_claims_fail_closed(provider,key,value):
    provider[1][key]=value
    with pytest.raises(HTTPException) as error:
        college.introspect("Bearer synthetic.access.token")
    assert error.value.status_code==401


@pytest.mark.parametrize("key",["COLLEGE_INTROSPECTION_URL","COLLEGE_ISSUER","COLLEGE_AUDIENCE","COLLEGE_CLIENT_ID","COLLEGE_CLIENT_SECRET"])
def test_missing_it_configuration_never_calls_provider(provider,monkeypatch,key):
    monkeypatch.delenv(key)
    with pytest.raises(HTTPException) as error:
        college.introspect("Bearer token")
    assert error.value.status_code==503
    provider[2].open.assert_not_called()


@pytest.mark.parametrize("endpoint",["https://@identity.example.invalid/","http://identity.example.invalid/introspect","https://user:pass@identity.example.invalid/","https://identity.example.invalid/?secret=value","https://identity.example.invalid/#fragment"])
def test_unsafe_endpoint_is_rejected(provider,monkeypatch,endpoint):
    monkeypatch.setenv("COLLEGE_INTROSPECTION_URL",endpoint)
    with pytest.raises(HTTPException) as error:
        college.introspect("Bearer token")
    assert error.value.status_code==503
    provider[2].open.assert_not_called()


def test_provider_failure_never_exposes_credentials(provider):
    provider[2].open.side_effect=RuntimeError("synthetic-secret Bearer sensitive")
    with pytest.raises(HTTPException) as error:
        college.introspect("Bearer synthetic.access.token")
    assert error.value.status_code==503
    assert "synthetic" not in error.value.detail


def test_redirect_is_always_rejected():
    with pytest.raises(ValueError):
        college.NoRedirect().redirect_request(None,None,None,None,None,None)


def test_college_identity_mapping_controls_local_role(provider):
    request=Request({"type":"http","headers":[],"client":("198.51.100.1",1234)})
    provider[1]["roles"]=["ADMIN"]
    db=Mock();user=User(role="STUDENT",is_active=True)
    db.scalar.return_value=user
    assert get_current_user(request,db=db,authorization="Bearer token") is user
    sql=db.scalar.call_args.args[0]
    assert "external_identities" in str(sql)
    assert set(sql.compile().params.values())=={provider[0]["COLLEGE_ISSUER"],"subject-1"}
    db.scalar.return_value=None
    with pytest.raises(HTTPException) as error:
        get_current_user(request,db=db,authorization="Bearer token")
    assert error.value.status_code==403


def test_college_mode_never_uses_dev_header(provider):
    db=Mock()
    with pytest.raises(HTTPException) as error:
        get_current_user(Mock(),x_dev_user="DEV-ADMIN",db=db,authorization="Bearer token")
    assert error.value.status_code==401
    provider[2].open.assert_not_called();db.scalar.assert_not_called()



def test_required_college_scope_is_enforced(provider,monkeypatch):
    monkeypatch.setenv("COLLEGE_REQUIRED_SCOPES","inventory.read")
    with pytest.raises(HTTPException) as error:
        college.introspect("Bearer token")
    assert error.value.status_code==401
    provider[1]["scope"]="inventory.read other"
    assert college.introspect("Bearer token")[1]=="subject-1"
