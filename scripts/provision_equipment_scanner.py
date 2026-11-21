"""Interactive, administrator-only provisioning for the isolated NFC demo database.

Importing this module never opens a connection. No runtime application grants or
migrations are changed by this tool. See docs/backend/SCANNER_PROVISIONING.md.
"""
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from pathlib import Path
import secrets
import sys
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from backend.alembic.application_safety import verify_application
from backend.app.auth.scanners import token_digest
from scripts import deploy_nfc_demo as baseline


class ProvisioningError(RuntimeError):
    """Only fixed, credential-free diagnostics may be displayed."""


@dataclass(frozen=True)
class Request:
    operation: str
    scanner_id: UUID | None = None
    staff_user_id: UUID | None = None
    expected_session_id: UUID | None = None
    minutes: int = 15
    dry_run: bool = False

    def validate(self):
        if self.operation not in {"register", "start-session", "renew-session", "revoke"}:
            raise ProvisioningError("Unsupported provisioning operation")
        for value in (self.scanner_id, self.staff_user_id, self.expected_session_id):
            if value is not None and not isinstance(value, UUID):
                raise ProvisioningError("Invalid identifier")
        if type(self.minutes) is not int or not 1 <= self.minutes <= 15:
            raise ProvisioningError("Session duration must be between 1 and 15 minutes")
        session = self.operation in {"start-session", "renew-session"}
        if (self.scanner_id is not None) != (self.operation != "register"):
            raise ProvisioningError("Scanner identifier is required only for existing scanners")
        if (self.staff_user_id is not None) != session:
            raise ProvisioningError("Staff identifier is required only for session operations")
        if (self.expected_session_id is not None) != (self.operation == "renew-session"):
            raise ProvisioningError("Renewal requires the current session identifier")


@dataclass(frozen=True)
class Result:
    operation: str
    scanner_id: UUID | None
    session_id: UUID | None = None
    staff_user_id: UUID | None = None
    expires_at: datetime | None = None
    secret: str | None = field(default=None, repr=False)
    dry_run: bool = False


def _secret(connection, schema):
    # Check both credential namespaces; never reuse a device key as a capture key.
    for _ in range(4):
        secret = secrets.token_urlsafe(32)
        digest = token_digest(secret)
        exists = connection.execute(text(
            f'SELECT EXISTS (SELECT 1 FROM "{schema}".equipment_scanners '
            'WHERE secret_sha256=:digest OR capture_secret_sha256=:digest)'
        ), {"digest": digest}).scalar_one()
        if not exists:
            return secret, digest
    raise ProvisioningError("Credential generation failed")


def apply_provisioning(connection, settings, request):
    """Apply within a caller-owned transaction; caller must not publish before commit.

    Public only for the disposable harness. Normal operators use the interactive CLI.
    Identity and the complete deployment protections are checked before any writes.
    """
    settings.validate()
    request.validate()
    if not connection.in_transaction():
        raise ProvisioningError("An explicit transaction is required")
    if request.dry_run:
        connection.execute(text("SET TRANSACTION READ ONLY"))
    baseline.check_connection(connection, settings, settings.administrator)
    baseline.acquire_stage_lock(connection)
    verify_application(connection, settings)
    schema = settings.schema  # validated identifier, never supplied as arbitrary SQL
    table = f'"{schema}".equipment_scanners'
    scanner = None
    if request.scanner_id is not None:
        scanner = connection.execute(text(
            f'SELECT * FROM {table} WHERE id=:id' + ("" if request.dry_run else " FOR UPDATE")
        ), {"id": request.scanner_id}).mappings().one_or_none()
        if scanner is None:
            raise ProvisioningError("Scanner does not exist")
    session = request.operation in {"start-session", "renew-session"}
    if session:
        staff = connection.execute(text(
            f'SELECT role, is_active FROM "{schema}".users WHERE id=:id'
            + ("" if request.dry_run else " FOR SHARE")
        ), {"id": request.staff_user_id}).mappings().one_or_none()
        if staff is None or not staff["is_active"] or staff["role"] not in {"FACULTY", "LAB_ASSISTANT"}:
            raise ProvisioningError("An existing active staff identity is required")
        if not scanner["enabled"]:
            raise ProvisioningError("Scanner is revoked")
    now = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    if request.operation == "start-session" and scanner["capture_session_id"] is not None:
        if scanner["capture_expires_at"] > now:
            raise ProvisioningError("An unexpired binding exists; use renewal")
    if request.operation == "renew-session" and scanner["capture_session_id"] != request.expected_session_id:
        raise ProvisioningError("Current binding changed; inspect before renewing")
    if request.dry_run:
        return Result(request.operation, request.scanner_id, dry_run=True)
    if request.operation == "register":
        scanner_id = uuid4()
        secret, digest = _secret(connection, schema)
        connection.execute(text(f'INSERT INTO {table} (id, secret_sha256) VALUES (:id, :digest)'),
                           {"id": scanner_id, "digest": digest})
        result = Result(request.operation, scanner_id, secret=secret)
    elif session:
        session_id = uuid4()
        if session_id == scanner["capture_session_id"]:
            raise ProvisioningError("Session identifier generation failed")
        secret, digest = _secret(connection, schema)
        expires = now + timedelta(minutes=request.minutes)
        connection.execute(text(f'UPDATE {table} SET capture_session_id=:session, capture_user_id=:staff, '
                                'capture_secret_sha256=:digest, capture_started_at=:started, '
                                'capture_expires_at=:expires WHERE id=:id'),
                           {"session": session_id, "staff": request.staff_user_id, "digest": digest,
                            "started": now, "expires": expires, "id": request.scanner_id})
        result = Result(request.operation, request.scanner_id, session_id, request.staff_user_id, expires, secret)
    else:
        connection.execute(text(f'UPDATE {table} SET enabled=false, capture_session_id=NULL, '
                                'capture_user_id=NULL, capture_secret_sha256=NULL, capture_started_at=NULL, '
                                'capture_expires_at=NULL WHERE id=:id'), {"id": request.scanner_id})
        result = Result(request.operation, request.scanner_id)
    verify_application(connection, settings)
    baseline.check_connection(connection, settings, settings.administrator)
    return result


def provision(settings, request, *, engine_factory=baseline.make_engine,
              prompt=baseline.secure_password, confirm=input):
    settings.validate()
    request.validate()
    label = (f'{request.operation} {request.scanner_id or "NEW"} '
             f'{settings.database}@{settings.server_address}:{settings.port}/{settings.schema}')
    if request.staff_user_id is not None:
        label += f' staff={request.staff_user_id} minutes={request.minutes}'
    if request.expected_session_id is not None:
        label += f' replacing={request.expected_session_id}'
    expected = f'PROVISION {label}'
    if confirm(f'Type {expected} to confirm: ') != expected:
        raise ProvisioningError("Provisioning not confirmed")
    engine = None
    try:
        password = prompt("Deployment administrator password (hidden): ")
        if not password:
            raise ProvisioningError("An interactive administrator password is required")
        engine = engine_factory(settings, settings.administrator, password)
        password = None
        with engine.connect() as connection:
            with connection.begin():
                result = apply_provisioning(connection, settings, request)
        # A commit acknowledgement is mandatory before any secret is returned.
        return result
    except (Exception, KeyboardInterrupt):
        raise ProvisioningError("Provisioning stopped; reconcile target state before retrying") from None
    finally:
        if engine is not None:
            engine.dispose()


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid arguments, which might contain a secret.
        self.exit(2, "Invalid provisioning arguments; use --help.\n")


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("operation", choices=["register", "start-session", "renew-session", "revoke"])
    parser.add_argument("--database", required=True, choices=[baseline.DATABASE])
    parser.add_argument("--server-address", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--administrator", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--scanner-id", type=UUID)
    parser.add_argument("--staff-user-id", type=UUID)
    parser.add_argument("--expected-session-id", type=UUID)
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            raise ProvisioningError("Interactive input and output terminals required")
        settings = baseline.Settings(args.database, args.server_address, args.port, args.administrator, args.schema)
        request = Request(args.operation, args.scanner_id, args.staff_user_id,
                          args.expected_session_id, args.minutes, args.dry_run)
        result = provision(settings, request)
        if result.dry_run:
            print("Dry run validated; no writes or credentials generated.")
        else:
            print(f'Committed {result.operation}; scanner {result.scanner_id}')
            if result.operation == "register":
                print(f'Authorization: Scanner {result.scanner_id}.{result.secret}')
            elif result.session_id is not None:
                print(f'Capture session: {result.session_id}; owner: {result.staff_user_id}; expires: {result.expires_at}')
                print(f'X-Scan-Session-Token: {result.secret}')
        return 0
    except (Exception, KeyboardInterrupt):
        print("Provisioning stopped. Details withheld; reconcile target state before retrying.", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())
