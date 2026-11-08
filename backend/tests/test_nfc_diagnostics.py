"""Diagnostic redaction tests; no Docker or database access."""
import json
from types import SimpleNamespace

from infra.nfc_postgres import diagnostics


def info(error):
    return SimpleNamespace(value=error, traceback=[
        SimpleNamespace(path=diagnostics.ROOT / "scripts/deploy_nfc_demo.py", lineno=30),
        SimpleNamespace(path=diagnostics.ROOT / "untrusted-secret-path.py", lineno=999)])


def test_no_arbitrary_exception_or_parameter_data_is_rendered():
    secret = "SYNTHETIC_PASSWORD_AND_TOKEN"
    error = RuntimeError("postgresql://user:" + secret + "@unrelated/db")
    record = diagnostics.failure(info(error), "test_successful_staged_deployment_and_verify_only_rerun[" + secret + "]", "call")
    output = json.dumps(record)
    assert secret not in output and "postgresql://" not in output
    assert "untrusted-secret-path" not in output
    assert record["reason"] == "unclassified"
    assert record["frames"] == [{"file": "scripts/deploy_nfc_demo.py", "line": 31}]


def test_fixed_repository_reason_and_sqlstate_are_preserved():
    error = RuntimeError("Column type or nullability mismatch")
    error.orig = SimpleNamespace(sqlstate="42501")
    record = diagnostics.failure(info(error), "untrusted_test[secret]", "setup")
    assert record["reason"] == "Column type or nullability mismatch"
    assert record["sqlstate"] == "42501"
    assert record["test"] == "unknown_test"


def test_does_not_call_exception_str_or_repr():
    class Dangerous(Exception):
        def __str__(self):
            raise AssertionError("Exception must not be rendered")
        def __repr__(self):
            raise AssertionError("Exception must not be rendered")
    error = Dangerous("SYNTHETIC_SECRET")
    error.orig = SimpleNamespace(sqlstate="PASSWORD")
    record = diagnostics.failure(info(error), "secret", "arbitrary_secret")
    assert record["exception"] == "OtherError"
    assert "sqlstate" not in record
    assert record["phase"] == "unknown"


def test_identity_queries_use_postgresql_host_address_form():
    for path in ("infra/nfc_postgres/harness.py", "scripts/deploy_nfc_demo.py"):
        source = (diagnostics.ROOT / path).read_text()
        assert "host(inet_server_addr())" in source
        assert "inet_server_addr()::text" not in source


def test_audit_schema_privilege_checks_bind_name_not_quoted_identifier(monkeypatch):
    import importlib
    from unittest.mock import Mock
    migration = importlib.import_module("backend.alembic.versions.c4e8a17d92f6_metadata_audit")
    operation = Mock()
    operation.get_context.return_value.opts = {}
    monkeypatch.setattr(migration, "op", operation)
    c = Mock()
    c.execute.return_value.one_or_none.side_effect = [(False, False, False), (False, False, False), (True, False, False)]
    c.scalar.side_effect = [False, True]  # no memberships; all source restrictions hold
    assert migration._writer(c, "public", '"public"') == (migration.WRITER, True)
    query, parameters = c.scalar.call_args.args
    assert parameters == {"schema_name": "public"}
    assert str(query).count(":schema_name") == 2
