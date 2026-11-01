from ipaddress import ip_address

from fastapi import HTTPException, Request, status

from backend.app.auth.config import is_dev_auth_enabled
from backend.app.models.user import UserRole


def require_local_dev_auth(request: Request) -> None:
    """Restrict development authentication to local requests."""

    if not is_dev_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Development authentication is disabled",
        )

    client_host = request.client.host if request.client else None

    try:
        is_loopback = (
            client_host is not None
            and ip_address(client_host).is_loopback
        )
    except ValueError:
        is_loopback = False

    if not is_loopback:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Development authentication requires a loopback connection",
        )

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models.user import User


def get_current_user(
    request: Request,
    x_dev_user: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Resolve a fictional user for local development only."""

    require_local_dev_auth(request)

    if not x_dev_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Development user header is required",
        )

    allowed_test_users = {
    "DEV-ADMIN",
    "DEV-FACULTY",
    "DEV-ASSISTANT",
    "DEV-STUDENT",
}

    if x_dev_user not in allowed_test_users:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid development user",
        )

    user = db.scalar(
        select(User).where(User.college_id == x_dev_user)
    )

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid development user",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    return user

from collections.abc import Callable
from fastapi import Depends


def require_roles(*allowed_roles: UserRole) -> Callable:
    """Allow access only to users with permitted roles."""

    def check_role(
        current_user: User = Depends(get_current_user),
    ) -> User:

        if current_user.role not in {
            role.value for role in allowed_roles
        }:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )

        return current_user

    return check_role