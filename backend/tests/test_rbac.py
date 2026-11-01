from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from backend.app.auth.dependencies import require_roles
from backend.app.models.user import UserRole


@pytest.mark.parametrize(
    "user_role, expected_status",
    [
        ("ADMIN", None),
        ("FACULTY", None),
        ("LAB_ASSISTANT", 403),
        ("STUDENT", 403),
    ],
)
def test_role_permissions(user_role, expected_status):
    # Create a fictional user
    user = Mock()
    user.role = user_role

    # Allow only Admin and Faculty
    check_role = require_roles(
        UserRole.ADMIN,
        UserRole.FACULTY,
    )

    if expected_status is None:
        result = check_role(current_user=user)
        assert result == user

    else:
        with pytest.raises(HTTPException) as error:
            check_role(current_user=user)

        assert error.value.status_code == expected_status