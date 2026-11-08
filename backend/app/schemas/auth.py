"""Public identity view; excludes authentication secrets."""
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class CurrentUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    college_id: str
    full_name: str
    role: str
    is_active: bool
