"""NFC-only environment: no ordinary fallback or engine creation."""
from alembic import context
from backend.alembic.safety import required_identity, run_on_connection
from backend.app.base import Base
import backend.app.models  # noqa: F401

config = context.config
if context.is_offline_mode():
    raise RuntimeError("NFC demo migrations require an online identity check")
expected = required_identity(config)
if config.attributes.get("metadata_audit_isolated_test"):
    raise RuntimeError("Isolated-test mode is forbidden for NFC demo deployment")
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("NFC demo migrations require an externally supplied connection")
run_on_connection(context, connection, Base.metadata, expected)
