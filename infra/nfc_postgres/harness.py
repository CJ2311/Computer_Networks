"""Disposable-instance controller. Importing this module never connects."""
from contextlib import contextmanager
from pathlib import Path
import os
import secrets
import stat
import uuid

import psycopg
from psycopg import sql

from scripts import deploy_nfc_demo as runner

OPT_IN = "YES_ISOLATED_PG18"
ADMIN = "nfc_controller"
SECRET_ROOT = Path("/run/nfc-harness")
WORKER_UID = WORKER_GID = 18085
SECRET_GID = 18084


class IsolationError(RuntimeError):
    pass


def require_opt_in():
    if os.environ.get("NFC_DISPOSABLE_OPT_IN") != OPT_IN or not Path("/.dockerenv").is_file():
        raise IsolationError("Disposable container opt-in required")
    if any(key.startswith("PG") for key in os.environ):
        raise IsolationError("Ambient PostgreSQL configuration is forbidden")
    if (getattr(os, "geteuid", lambda: None)() != WORKER_UID
            or getattr(os, "getegid", lambda: None)() != WORKER_GID
            or SECRET_GID not in getattr(os, "getgroups", lambda: [])()):
        raise IsolationError("Dedicated non-root worker identity and secrets group required")


def validate_identity_permissions(directory, credential):
    """The worker may read these files but must not own or modify them."""
    if (not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o750
            or directory.st_gid != SECRET_GID or directory.st_uid in {0, WORKER_UID}
            or not stat.S_ISREG(credential.st_mode) or stat.S_IMODE(credential.st_mode) != 0o440
            or credential.st_gid != SECRET_GID or credential.st_uid != directory.st_uid):
        raise IsolationError("Unsafe shared identity permissions")


def read_identity_file(name):
    if name not in {"run_id", "admin_password"} or not hasattr(os, "O_NOFOLLOW"):
        raise IsolationError("Unsupported shared identity access")
    directory = SECRET_ROOT.lstat()
    # Check the actual open descriptor, not a separate path followed by a read.
    descriptor = os.open(SECRET_ROOT / name, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="ascii") as stream:
        validate_identity_permissions(directory, os.fstat(stream.fileno()))
        return stream.read().strip()


class Harness:
    def __init__(self):
        require_opt_in()
        self.run_id = str(uuid.UUID(read_identity_file("run_id")))
        self.admin_password = read_identity_file("admin_password")
        if len(self.admin_password) != 96:
            raise IsolationError("Fresh disposable credentials required")
        self.system_identifier = None
        self.settings = runner.Settings(runner.DATABASE, "127.0.0.1", 5432, ADMIN)
        self.passwords = {ADMIN: self.admin_password,
                          runner.MIGRATOR: secrets.token_hex(32), "inventory_app": secrets.token_hex(32)}
        self.engines = []

    @contextmanager
    def control(self):
        require_opt_in()
        with psycopg.connect(host="127.0.0.1", port=5432, dbname="postgres", user=ADMIN,
                password=self.admin_password, autocommit=True, connect_timeout=5,
                application_name="nfc-harness-controller", options="-csearch_path=pg_catalog") as c:
            self.assert_identity(c)
            yield c

    def assert_identity(self, connection):
        row = connection.execute("""SELECT current_database(), session_user, current_user,
            host(inet_server_addr()), inet_server_port(), current_setting('server_version_num')::int,
            current_setting('cluster_name'), system_identifier::text,
            current_setting('data_directory') FROM pg_control_system()""").fetchone()
        if (row[:6] != ("postgres", ADMIN, ADMIN, "127.0.0.1", 5432, 180004)
                or row[6] != "nfc-disposable-" + self.run_id
                or not row[8].startswith("/var/lib/postgresql/18/")):
            raise IsolationError("Disposable instance identity mismatch")
        if self.system_identifier is None:
            self.system_identifier = row[7]
        if row[7] != self.system_identifier:
            raise IsolationError("PostgreSQL instance changed")

    def bootstrap(self):
        with self.control() as c:
            databases = {r[0] for r in c.execute("SELECT datname FROM pg_database").fetchall()}
            if databases != {"postgres", "template0", "template1"}:
                raise IsolationError("Fresh instance required; refusing existing databases")
            roles = {r[0] for r in c.execute("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_'").fetchall()}
            if roles != {ADMIN}:
                raise IsolationError("Fresh instance-local roles required")
            for role in (runner.MIGRATOR, "inventory_app", "inventory_metadata_audit_owner", "inventory_metadata_audit_writer"):
                self.assert_identity(c)
                login = role in self.passwords
                statement = sql.SQL("CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(
                    sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN"))
                if login:
                    statement += sql.SQL(" PASSWORD {}").format(sql.Literal(self.passwords[role]))
                c.execute(statement)

    def reset_database(self):
        self.dispose()
        with self.control() as c:
            exists = c.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
                               (runner.DATABASE,)).fetchone()
            if exists:
                if exists[0] != "nfc-harness:" + self.run_id:
                    raise IsolationError("Refusing cleanup of an unowned database")
                self.assert_identity(c)
                c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(runner.DATABASE)))
            self.assert_identity(c)
            c.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(runner.DATABASE), sql.Identifier(ADMIN)))
            c.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(runner.DATABASE), sql.Literal("nfc-harness:" + self.run_id)))
        with self.engine(ADMIN).begin() as connection:
            # Database creation and schema setup stay on the verified instance.
            self.verify_engine_identity(connection)
            connection.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            connection.exec_driver_sql("GRANT USAGE, CREATE ON SCHEMA public TO inventory_migrator")
            connection.exec_driver_sql("GRANT USAGE ON SCHEMA public TO inventory_app")

    def verify_engine_identity(self, connection):
        # Controller revalidates system ID before any harness mutation. The
        # migration connection itself must also match the runner's actual target.
        with self.control():
            pass
        user = connection.engine.url.username
        runner.check_connection(connection, self.settings, user) if user != "inventory_app" else runner.verify_identity(
            connection, runner.identity(self.settings, user))
        from sqlalchemy import text
        marker = connection.scalar(text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"))
        if marker != "nfc-harness:" + self.run_id:
            raise IsolationError("Unowned test database")

    def engine(self, user=runner.MIGRATOR):
        engine = runner.make_engine(self.settings, user, self.passwords[user])
        self.engines.append(engine)
        return engine

    def deploy(self):
        def factory(settings, user, password):
            if settings != self.settings or password != self.passwords[user]:
                raise IsolationError("Unexpected deployment target or identity")
            with self.control():
                pass
            return self.engine(user)
        def prompt(message):
            return self.passwords[ADMIN if "administrator" in message else runner.MIGRATOR]
        runner.deploy(self.settings, engine_factory=factory, prompt=prompt,
                      confirm=lambda _: "INITIALIZE inventory_nfc_demo")

    def stage(self, before, after, user):
        with self.engine(user).connect() as connection:
            self.verify_engine_identity(connection)
            connection.rollback()
            runner.run_stage(connection, self.settings, user, before, after)

    def terminate(self, pid):
        with self.control() as c:
            row = c.execute("SELECT datname, application_name FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone()
            if row != (runner.DATABASE, "inventory_nfc_demo_deployment"):
                raise IsolationError("Refusing to terminate an unrelated backend")
            self.assert_identity(c)
            if not c.execute("SELECT pg_terminate_backend(%s,5000)", (pid,)).fetchone()[0]:
                raise IsolationError("Test backend termination failed")

    def dispose(self):
        for engine in self.engines:
            engine.dispose()
        self.engines.clear()
