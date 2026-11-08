"""Static and mocked checks only: never start Docker or connect to PostgreSQL."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import MagicMock
from types import SimpleNamespace
import stat
import os

import pytest
import yaml

from infra.nfc_postgres import harness

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra/nfc_postgres"


def test_compose_has_no_route_or_mount_to_existing_postgres():
    config = yaml.safe_load((INFRA / "compose.yaml").read_text())
    assert set(config["services"]) == {"postgres", "worker"}
    assert "networks" not in config
    pg, worker = config["services"]["postgres"], config["services"]["worker"]
    assert pg["network_mode"] == "none"
    assert worker["network_mode"] == "service:postgres"
    for service in (pg, worker):
        assert not any(key in service for key in ("networks", "ports", "env_file", "privileged", "extra_hosts", "pid", "cap_add", "links", "external_links", "dns"))
        assert "${NFC_DISPOSABLE_OPT_IN:?" in service["environment"]["NFC_DISPOSABLE_OPT_IN"]
        assert all(mount.startswith("identity:/run/nfc-harness") for mount in service["volumes"])
        assert "docker.sock" not in str(service)
    assert pg["tmpfs"] == ["/var/lib/postgresql:size=768m,mode=0700"]
    volume = config["volumes"]["identity"]
    assert volume["driver_opts"]["type"] == "tmpfs"
    assert not volume.get("external")
    assert not worker.get("networks")
    assert worker["read_only"] is True
    assert worker["user"] == "18085:18085"
    assert worker["group_add"] == ["18084"]
    assert worker["cap_drop"] == ["ALL"]
    assert worker["security_opt"] == ["no-new-privileges:true"]


def test_worker_installs_dependencies_only_at_build_time():
    config = yaml.safe_load((INFRA / "compose.yaml").read_text())
    worker = config["services"]["worker"]
    assert "command" not in worker and "entrypoint" not in worker
    dockerfile = (INFRA / "worker.Dockerfile").read_text()
    assert "RUN pip install --no-cache-dir -r /tmp/requirements.txt" in dockerfile
    assert 'ENTRYPOINT ["python", "-B", "-m", "infra.nfc_postgres.worker"]' in dockerfile
    assert "USER nfc-worker" in dockerfile
    assert "useradd --uid 18085 --gid 18085 --groups nfc-secrets" in dockerfile
    assert "groupadd --gid 18084 nfc-secrets" in dockerfile
    for name in ("worker.py", "harness.py"):
        source = (INFRA / name).read_text()
        assert "pip install" not in source
        assert "host=\"127.0.0.1\"" in source if name == "harness.py" else True


def test_documentation_requires_cleanup_label_verification():
    doc = (INFRA / "README.md").read_text()
    assert "network_mode: none" in doc
    assert "network_mode: service:postgres" in doc
    assert "Abort cleanup" in doc
    assert "docker inspect" in doc
    assert "docker volume inspect" in doc
    assert "com.docker.compose.project" in doc
    assert "org.inventory.harness" in doc
    assert "The server process inherits this environment" not in doc


def test_images_and_build_context_are_explicit():
    pg = (INFRA / "postgres.Dockerfile").read_text()
    worker = (INFRA / "worker.Dockerfile").read_text()
    assert "FROM postgres:18.4-bookworm" in pg
    assert "FROM python:3.12-slim-bookworm" in worker
    assert "COPY . " not in worker
    assert "database.py" not in worker
    assert "alembic_nfc_demo.ini" in worker
    assert "COPY backend/alembic backend/alembic" in worker
    for name in ("worker", "postgres"):
        ignore = (INFRA / f"{name}.Dockerfile.dockerignore").read_text()
        assert ignore.startswith("**\n")
    assert (INFRA / "worker.Dockerfile.dockerignore").read_text().rstrip().endswith("**/.env*")
    bootstrap = (INFRA / "bootstrap.sh").read_text()
    assert "/dev/urandom" in bootstrap
    assert "POSTGRES_PASSWORD_FILE" in bootstrap
    assert "listen_addresses=127.0.0.1" in bootstrap
    assert "YES_ISOLATED_PG18" in bootstrap
    assert "set -x" not in bootstrap


def test_no_opt_in_never_reads_secrets_or_connects(monkeypatch):
    monkeypatch.delenv("NFC_DISPOSABLE_OPT_IN", raising=False)
    connect = Mock(side_effect=AssertionError("No live connections"))
    monkeypatch.setattr(harness.psycopg, "connect", connect)
    with pytest.raises(harness.IsolationError):
        harness.Harness()
    connect.assert_not_called()


def test_opt_in_rejects_ambient_libpq_configuration(monkeypatch):
    monkeypatch.setenv("NFC_DISPOSABLE_OPT_IN", harness.OPT_IN)
    monkeypatch.setattr(harness.Path, "is_file", lambda _: True)
    monkeypatch.setenv("PGHOST", "unrelated-host")
    with pytest.raises(harness.IsolationError, match="Ambient"):
        harness.require_opt_in()


def controller():
    h = harness.Harness.__new__(harness.Harness)
    h.run_id = "11111111-1111-1111-1111-111111111111"
    h.system_identifier = None
    return h


def identity_row(h):
    return ["postgres", harness.ADMIN, harness.ADMIN, "127.0.0.1", 5432, 180004,
            "nfc-disposable-" + h.run_id, "synthetic-system-id", "/var/lib/postgresql/18/docker"]


@pytest.mark.parametrize("field,value", [(0, "inventory_db"), (0, "inventory_checkout_test"),
    (1, "postgres"), (3, "192.0.2.1"), (4, 5433), (5, 180005),
    (6, "unrelated-cluster"), (8, "/existing/pgdata")])
def test_controller_rejects_wrong_instance(field, value):
    h = controller()
    row = identity_row(h); row[field] = value
    c = Mock(); c.execute.return_value.fetchone.return_value = tuple(row)
    with pytest.raises(harness.IsolationError):
        h.assert_identity(c)
    assert c.execute.call_count == 1


def test_controller_pins_system_identifier():
    h = controller()
    row = identity_row(h)
    c = Mock(); c.execute.return_value.fetchone.return_value = tuple(row)
    h.assert_identity(c)
    row[7] = "another-system"
    c.execute.return_value.fetchone.return_value = tuple(row)
    with pytest.raises(harness.IsolationError, match="changed"):
        h.assert_identity(c)


def test_cleanup_rejects_unowned_database(monkeypatch):
    h = controller(); h.engines = []
    c = Mock(); c.execute.return_value.fetchone.return_value = ("unrelated marker",)
    @contextmanager
    def control():
        yield c
    monkeypatch.setattr(h, "control", control)
    with pytest.raises(harness.IsolationError, match="unowned"):
        h.reset_database()
    assert c.execute.call_count == 1  # no DROP or CREATE


def test_termination_rejects_unrelated_backend(monkeypatch):
    h = controller()
    c = Mock(); c.execute.return_value.fetchone.return_value = ("inventory_db", "unrelated")
    @contextmanager
    def control():
        yield c
    monkeypatch.setattr(h, "control", control)
    with pytest.raises(harness.IsolationError, match="unrelated backend"):
        h.terminate(123)
    assert c.execute.call_count == 1


def test_infrastructure_python_compiles_without_execution():
    for path in INFRA.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")


def test_integration_collection_gate_has_no_import_time_provisioning():
    source = (INFRA / "tests/conftest.py").read_text()
    assert "pytest_collection_modifyitems" in source
    assert "pytest.mark.skip" in source
    assert "require_opt_in()" in source
    assert "scope=\"session\"" in source
    assert "harness.bootstrap()" in source


def test_worker_only_outputs_counts():
    source = (INFRA / "worker.py").read_text()
    assert '"no:terminal"' in source
    assert "redirect_stdout" in source and "redirect_stderr" in source
    assert "json.dumps" in source
    assert "require_opt_in()" in source


def permission_pair():
    return (SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_gid=18084, st_uid=999),
            SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_gid=18084, st_uid=999))


def test_shared_credentials_are_readable_without_worker_ownership():
    directory, credential = permission_pair()
    harness.validate_identity_permissions(directory, credential)
    assert credential.st_uid != harness.WORKER_UID
    assert not credential.st_mode & 0o222


@pytest.mark.parametrize("which,attribute,value", [
    (0, "st_mode", stat.S_IFDIR | 0o755), (0, "st_mode", stat.S_IFLNK | 0o750),
    (0, "st_uid", 18085), (0, "st_gid", 999),
    (1, "st_mode", stat.S_IFREG | 0o444), (1, "st_mode", stat.S_IFREG | 0o400),
    (1, "st_mode", stat.S_IFREG | 0o660), (1, "st_mode", stat.S_IFLNK | 0o440),
    (1, "st_uid", 18085), (1, "st_gid", 999),
])
def test_rejects_unsafe_shared_secret_metadata(which, attribute, value):
    pair = permission_pair()
    setattr(pair[which], attribute, value)
    with pytest.raises(harness.IsolationError, match="permissions"):
        harness.validate_identity_permissions(*pair)


@pytest.mark.parametrize("uid,gid,groups,accepted", [
    (18085, 18085, [18084], True), (0, 0, [18084], False),
    (18085, 18085, [], False), (18085, 18084, [18084], False),
])
def test_worker_identity_gate(monkeypatch, uid, gid, groups, accepted):
    for key in list(os.environ):
        if key.startswith("PG"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NFC_DISPOSABLE_OPT_IN", harness.OPT_IN)
    monkeypatch.setattr(harness.Path, "is_file", lambda _: True)
    monkeypatch.setattr(harness.os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setattr(harness.os, "getegid", lambda: gid, raising=False)
    monkeypatch.setattr(harness.os, "getgroups", lambda: groups, raising=False)
    if accepted:
        harness.require_opt_in()
    else:
        with pytest.raises(harness.IsolationError, match="non-root"):
            harness.require_opt_in()


def test_secret_reader_validates_actual_descriptor_and_disallows_symlinks(monkeypatch):
    directory, credential = permission_pair()
    root = MagicMock()
    root.lstat.return_value = directory
    monkeypatch.setattr(harness, "SECRET_ROOT", root)
    opener = Mock(return_value=42)
    stream = MagicMock()
    stream.fileno.return_value = 42
    stream.read.return_value = "synthetic-value\n"
    context = MagicMock()
    context.__enter__.return_value = stream
    metadata = Mock(return_value=credential)
    # Patch only this module's OS dependency, never pytest's global os.fstat.
    monkeypatch.setattr(harness, "os", SimpleNamespace(
        O_NOFOLLOW=0x20000, O_RDONLY=os.O_RDONLY, open=opener,
        fdopen=Mock(return_value=context), fstat=metadata))
    assert harness.read_identity_file("admin_password") == "synthetic-value"
    assert opener.call_args.args[1] & harness.os.O_NOFOLLOW
    metadata.assert_called_once_with(42)
    context.__exit__.assert_called_once()
