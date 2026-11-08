from fastapi import APIRouter, Depends
from backend.app.auth.dependencies import get_current_user
from backend.app.models.user import User
from backend.app.schemas.auth import CurrentUserResponse


router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/me", response_model=CurrentUserResponse)
def current_identity(user: User = Depends(get_current_user)):
    return user
