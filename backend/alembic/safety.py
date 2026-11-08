import re
from sqlalchemy import text

def required_identity(config):
    """Require explicit deployment intent; neither .env nor URLs are intent.

    This environment is restricted to the NFC demo. Integration fixtures keep
    invoking revision functions directly, without this deployment environment.
    """
    identity = config.attributes.get("nfc_demo_identity")
    fields = {"database", "session_user", "current_user", "schema"}
    if not isinstance(identity, dict) or set(identity) != fields:
        raise RuntimeError("Explicit NFC demo database, session_user, current_user and schema required")
    if any(not isinstance(value, str) or not value.strip() for value in identity.values()):
        raise RuntimeError("NFC demo identity fields must be nonempty strings")
    if identity["database"] != "inventory_nfc_demo":
        raise RuntimeError("Only inventory_nfc_demo is approved for this migration environment")
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", identity["schema"]) or identity["schema"].startswith("pg_"):
        raise RuntimeError("A trusted application schema is required")
    return dict(identity)


def verify_identity(connection, expected):
    if connection.dialect.name != "postgresql":
        raise RuntimeError("NFC demo migrations require PostgreSQL")
    actual = connection.execute(text(
        "SELECT pg_catalog.current_database(), session_user, current_user, "
        "pg_catalog.current_schema(), pg_catalog.current_schemas(true)"
    )).one()
    # Reject alternate application schemas and temporary schemas in search_path.
    if tuple(actual[:4]) != tuple(expected[key] for key in (
        "database", "session_user", "current_user", "schema"
    )) or list(actual[4]) != ["pg_catalog", expected["schema"]]:
        raise RuntimeError("NFC demo connection identity or schema mismatch")


def run_on_connection(context, connection, metadata, expected=None):
    # Configure before SELECT's autobegin so Alembic correctly identifies whether
    # the caller already owns a transaction. The guard runs inside that scope.
    context.configure(connection=connection, target_metadata=metadata,
                      version_table_schema=expected["schema"] if expected else None)
    with context.begin_transaction():
        if expected is not None:
            verify_identity(connection, expected)
        context.run_migrations()
