"""Resolve a borrower through the sole runtime-authorized users write boundary."""
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, literal, select, text
from sqlalchemy.exc import DBAPIError

from backend.app.schemas.borrowers import BorrowerResolve, BorrowerResolveResponse
from backend.app.services.inventory_transactions import transaction
from backend.app.services.maintenance import _authorize
from backend.app.models.user import User


# Match the frozen borrower migration's expression index and Python strip()
# whitespace set. Inline this fixed SQL constant at execution so PostgreSQL can
# match the expression index even when using a generic prepared query plan.
REGISTRATION_TRIM_CHARACTERS = "".join(chr(code) for code in (
    *range(9, 14), *range(28, 33), 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
))


def _normalized_registration(value):
    return func.upper(func.btrim(
        value, literal(REGISTRATION_TRIM_CHARACTERS, literal_execute=True),
    ).collate("C"))


def lookup_borrower(db, college_id: str):
    """Read-only lookup; the existing router enforces staff authorization.

    Compare all normalized identities before checking borrower eligibility, so
    legacy/drifted data cannot silently select one of several matching UUIDs.
    """
    if not isinstance(college_id, str) or not 1 <= len(college_id) <= 50 or "\x00" in college_id:
        raise HTTPException(422, "Invalid registration number")
    with db.no_autoflush:
        matches = db.scalars(select(User).where(
            _normalized_registration(User.college_id) == _normalized_registration(college_id),
        ).limit(2)).all()
        if len(matches) > 1:
            raise HTTPException(409, "Registration number conflicts with multiple identities")
        if not matches or matches[0].role != "STUDENT" or not matches[0].is_active:
            raise HTTPException(404, "Active student not found")
        return matches[0]


@transaction(uncertain_commit_detail="Borrower resolution outcome is uncertain; retry with the same name and registration number")
def resolve_borrower(db, data: BorrowerResolve, actor):
    return resolve_borrower_in_transaction(db, data, actor)


def resolve_borrower_in_transaction(db, data: BorrowerResolve, actor):
    """Compose borrower resolution within an already-owned inventory transaction."""
    from backend.app.services.inventory_transactions import require_owner
    require_owner(db)
    data = BorrowerResolve.model_validate(data)
    staff = _authorize(db, actor, {"FACULTY", "LAB_ASSISTANT"})
    schema = db.scalar(select(func.current_schema()))
    if not isinstance(schema, str) or not re.fullmatch(r"[a-z_][a-z_0-9]*", schema) or schema.startswith("pg_"):
        raise HTTPException(503, "Borrower registration is unavailable")
    qualified = db.get_bind().dialect.identifier_preparer.quote_schema(schema)
    try:
        row = db.execute(text(f"""SELECT id, college_id, full_name, created
            FROM {qualified}.borrower_resolve_or_create(:actor, :new_id, :name, :registration)"""),
            {"actor": staff.id, "new_id": uuid4(), "name": data.student_name,
             "registration": data.registration_number}).mappings().one()
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None)
        if code == "P2001":
            raise HTTPException(409, "Registration number conflicts with an existing identity") from None
        if code == "42501":
            raise HTTPException(403, "Active authorized staff required") from None
        if code == "22023":
            raise HTTPException(422, "Invalid borrower details") from None
        raise
    return BorrowerResolveResponse(student_id=row["id"], student_name=row["full_name"],
                                   registration_number=data.registration_number, created=row["created"])
