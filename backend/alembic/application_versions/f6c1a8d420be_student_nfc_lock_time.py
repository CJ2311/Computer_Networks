"""Evaluate student-card challenge deadlines after acquiring row locks."""
from importlib import import_module
import re

from alembic import op
from sqlalchemy import text

revision = "f6c1a8d420be"
down_revision = "a9c2e4f610bd"
branch_labels = None
depends_on = None

PREVIOUS = import_module("backend.alembic.versions.e4b7c9d210af_student_nfc")
OWNER = PREVIOUS.OWNER
SIGNATURE = PREVIOUS.SIGNATURE


def routine_body(q):
    # Derive from the frozen predecessor to keep every non-timing guard intact.
    # Fail closed if an anchor ever drifts; never silently install a partial fix.
    body = PREVIOUS.routine_body(q)
    replacements = (
        ("at_time timestamptz := clock_timestamp()", "at_time timestamptz"),
        ("WHERE student_id=v_student FOR UPDATE;\n        IF FOUND THEN",
         "WHERE student_id=v_student FOR UPDATE;\n        at_time := clock_timestamp();\n        IF FOUND THEN"),
        ("    IF r.status='PENDING_WRITE' AND r.expires_at<=at_time THEN",
         "    at_time := clock_timestamp();\n    IF r.status='PENDING_WRITE' AND r.expires_at<=at_time THEN"),
    )
    for before, after in replacements:
        if body.count(before) != 1:
            raise RuntimeError("Unexpected student NFC predecessor routine")
        body = body.replace(before, after, 1)
    return body


def upgrade():
    # Only trusted staged deployment may replace this protected routine. No
    # source rows, table definitions, grants, or transaction boundaries are changed.
    c = op.get_bind()
    if c.dialect.name != "postgresql" or c.scalar(text(
        "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
    )) is not True:
        raise RuntimeError("Student NFC timing migration requires a trusted deployment administrator")
    schema = c.scalar(text("SELECT current_schema()"))
    if not schema or not re.fullmatch(r"[a-z_][a-z_0-9]*", schema) or schema.startswith("pg_"):
        raise RuntimeError("Trusted application schema required")
    q = c.dialect.identifier_preparer.quote_schema(schema)
    from backend.alembic.student_nfc_safety import verify_student_nfc
    verify_student_nfc(c, q, routine_migration=PREVIOUS)
    # PostgreSQL CREATE OR REPLACE retains existing ownership and execution ACLs.
    op.execute(f"""CREATE OR REPLACE FUNCTION {q}.student_nfc_manage(
        p_action text,p_actor uuid,p_id uuid,p_data jsonb)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $studentnfc${routine_body(q)}$studentnfc$""")
    verify_student_nfc(c, q)


def downgrade():
    raise RuntimeError("Student NFC timing protection requires a separately reviewed forward migration")
