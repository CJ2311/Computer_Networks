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