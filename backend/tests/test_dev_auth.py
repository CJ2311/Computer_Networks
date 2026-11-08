from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from backend.app.auth.dependencies import require_local_dev_auth


@pytest.mark.parametrize(
    "client_ip, expected_status",
    [
        ("127.0.0.1", None),
        ("::1", None),
        ("192.168.1.100", 403),
        ("8.8.8.8", 403),
        ("invalid-ip", 403),
    ],
)
def test_dev_auth_loopback(client_ip, expected_status, monkeypatch):
    monkeypatch.setattr(
        "backend.app.auth.dependencies.is_dev_auth_enabled",
        lambda: True,
    )

    request = Mock()
    request.client.host = client_ip

    if expected_status is None:
        require_local_dev_auth(request)
    else:
        with pytest.raises(HTTPException) as error:
            require_local_dev_auth(request)

        assert error.value.status_code == expected_status


def test_dev_auth_disabled(monkeypatch):
    monkeypatch.setattr(
        "backend.app.auth.dependencies.is_dev_auth_enabled",
        lambda: False,
    )

    request = Mock()
    request.client.host = "127.0.0.1"

    with pytest.raises(HTTPException) as error:
        require_local_dev_auth(request)

    assert error.value.status_code == 403

from unittest.mock import patch

from backend.app.auth.dependencies import get_current_user
from backend.app.models.user import User


def test_get_current_dev_admin():
    request = Mock()
    request.client.host = "127.0.0.1"

    db = Mock()
    db.scalar.return_value = User(college_id="DEV-ADMIN", role="ADMIN", is_active=True)
    with patch("backend.app.auth.dependencies.is_dev_auth_enabled", return_value=True):
        user = get_current_user(request=request, x_dev_user="DEV-ADMIN", db=db)

    assert user.college_id == "DEV-ADMIN"
    assert user.role == "ADMIN"
    assert user.is_active is True
    db.scalar.assert_called_once()

@pytest.mark.parametrize(
    "dev_user",
    [
        None,
        "DEV-UNKNOWN",
        "CB.EN.U4CSE12345",
    ],
)
def test_invalid_dev_users(dev_user):
    request = Mock()
    request.client.host = "127.0.0.1"

    db = Mock()
    with patch("backend.app.auth.dependencies.is_dev_auth_enabled", return_value=True):
        with pytest.raises(HTTPException) as error:
            get_current_user(request=request, x_dev_user=dev_user, db=db)

    assert error.value.status_code == 401
    db.scalar.assert_not_called()

def test_inactive_dev_user():
    request = Mock()
    request.client.host = "127.0.0.1"

    mock_db = Mock()

    inactive_user = Mock()
    inactive_user.college_id = "DEV-STUDENT"
    inactive_user.is_active = False

    mock_db.scalar.return_value = inactive_user

    with patch(
        "backend.app.auth.dependencies.is_dev_auth_enabled",
        return_value=True,
    ):
        with pytest.raises(HTTPException) as error:
            get_current_user(
                request=request,
                x_dev_user="DEV-STUDENT",
                db=mock_db,
            )

    assert error.value.status_code == 403
    assert error.value.detail == "User account is inactive"
