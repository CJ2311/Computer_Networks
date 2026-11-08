"""Run only a path-rewritten bootstrap with stubbed PostgreSQL/ownership tools."""
import os
from pathlib import Path
import shutil
import subprocess
import re
import shlex

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "infra/nfc_postgres/bootstrap.sh"


@pytest.fixture(scope="session")
def posix_shell():
    candidates = [shutil.which("sh")]
    if Path("C:/Program Files/Git/bin/bash.exe").exists():
        candidates.append("C:/Program Files/Git/bin/bash.exe")
    shell = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            probe = subprocess.run([candidate, "-c", "exit 0"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            shell = candidate
            break
    if not shell:
        pytest.skip("POSIX shell required; no Docker fallback")
    return shell


@pytest.fixture
def bootstrap(tmp_path, posix_shell):
    shell = posix_shell
    identity = tmp_path / "identity"
    identity.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    status = tmp_path / "status"
    entrypoint = tmp_path / "entrypoint"
    hook = tmp_path / "clear-init-passwords.sh"
    hook.write_text((SOURCE.parent / "clear-init-passwords.sh").read_text(), newline="\n")
    # Never invoke postgres, initdb, Docker, psql, or migrations.
    # Model upstream file_env + init-only PGPASSWORD, then source the real hook
    # in this same shell to check final-process environment cleanup.
    entrypoint.write_text('''#!/bin/sh
set -eu
[ "${PGPASSWORD+x}" != x ] || exit 90
[ "${POSTGRES_PASSWORD+x}" != x ] || exit 91
POSTGRES_PASSWORD=$(cat "$POSTGRES_PASSWORD_FILE")
export POSTGRES_PASSWORD
[ "${#POSTGRES_PASSWORD}" -eq 96 ] || exit 92
export PGPASSWORD="$POSTGRES_PASSWORD"
''' + f'. "{hook.as_posix()}"\n' + '''
[ "${PGPASSWORD+x}" != x ] || exit 93
[ "${POSTGRES_PASSWORD+x}" != x ] || exit 94
[ "${POSTGRES_PASSWORD_FILE+x}" != x ] || exit 95
[ "$POSTGRES_USER" = nfc_controller ] || exit 96
[ "$POSTGRES_HOST_AUTH_METHOD" = scram-sha-256 ] || exit 97
''', newline="\n")
    # Fixed container paths have no spaces; rewritten Windows temp paths do.
    # Quote each prefix while retaining the original script's path suffixes.
    script = SOURCE.read_text().replace("/run/nfc-startup-status", shlex.quote(status.as_posix()))
    script = script.replace("/run/nfc-harness", shlex.quote(identity.as_posix()))
    script = script.replace("/var/lib/postgresql", shlex.quote(data.as_posix()))
    script = script.replace("/usr/local/bin/docker-entrypoint.sh",
                            f'{shlex.quote(Path(shell).as_posix())} {shlex.quote(entrypoint.as_posix())}')
    script = 'chown() { return 0; }\n' + script
    # Git's Windows shell has no Linux /proc UUID source.
    script = script.replace("cat /proc/sys/kernel/random/uuid", "printf 'test-run-id\\n'")

    def run(*, opt_in=True, fail=None, mutate=None, inherited=None):
        source = script
        if fail:
            if fail == "cat /proc/sys/kernel/random/uuid":
                fail = "printf 'test-run-id\\n'"
            # Inject a command failure at a known filesystem operation.
            if fail == "od -An":
                source = source.replace("od -An -N48 -tx1 /dev/urandom", "false")
            else:
                source = source.replace(fail, "false # " + fail)
        if mutate:
            source = mutate(source)
        path = tmp_path / "bootstrap.sh"
        path.write_text(source, newline="\n")
        env = {"PATH": os.environ["PATH"], "PGDATA": data.as_posix() + "/18/docker"}
        env.update(inherited or {})
        if opt_in:
            env["NFC_DISPOSABLE_OPT_IN"] = "YES_ISOLATED_PG18"
        result = subprocess.run([shell, str(path)], env=env, capture_output=True, text=True, timeout=10)
        if not status.exists():
            # No bootstrap stage ran; no generated password exists to expose.
            pytest.fail(f"Bootstrap stub did not start (exit {result.returncode}): {result.stderr}")
        diagnostic = status.read_text()
        assert re.fullmatch(r"stage=(?:opt-in|reuse-check|password-write|password-validate|run-id-write|identity-permissions|data-directory|run-id-read|postgres-entrypoint)(?: exit=[0-9]+)?\n", diagnostic)
        return result, diagnostic

    return run, identity


def test_bootstrap_handoff_with_local_scram_password(bootstrap):
    run, identity = bootstrap
    result, status = run()
    assert result.returncode == 0, result.stderr
    assert status == "stage=postgres-entrypoint\n"
    secret = (identity / "admin_password").read_text()
    assert len(secret) == 96
    assert secret not in status + result.stdout + result.stderr


def test_bootstrap_opt_in_is_required(bootstrap):
    run, identity = bootstrap
    result, status = run(opt_in=False)
    assert result.returncode == 70
    assert status == "stage=opt-in exit=70\n"
    assert not list(identity.iterdir())


@pytest.mark.parametrize("name", ["admin_password", "run_id"])
def test_bootstrap_refuses_partial_or_complete_identity(bootstrap, name):
    run, identity = bootstrap
    (identity / name).write_text("existing")
    result, status = run()
    assert result.returncode == 71
    assert status == "stage=reuse-check exit=71\n"
    assert (identity / name).read_text() == "existing"


@pytest.mark.parametrize("command,stage", [
    ("od -An", "password-validate"),  # pipeline's last command still succeeds
    ("cat /proc/sys/kernel/random/uuid", "run-id-write"),
    ("chmod 0440", "identity-permissions"),
    ("chmod 0750", "identity-permissions"),
    ("chown postgres:18084", "identity-permissions"),
    ("chown postgres:postgres", "data-directory"),
    ("mkdir -p", "data-directory"),
])
def test_bootstrap_records_filesystem_failure(bootstrap, command, stage):
    run, _ = bootstrap
    result, status = run(fail=command)
    assert result.returncode != 0
    assert status.startswith(f"stage={stage} exit=")


def test_bootstrap_records_password_redirection_failure(bootstrap):
    run, identity = bootstrap
    identity.rmdir()
    result, status = run()
    assert result.returncode != 0
    assert status.startswith("stage=password-write exit=")


def test_bootstrap_rejects_unexpected_pgdata(bootstrap):
    run, _ = bootstrap
    result, status = run(mutate=lambda source: source.replace(
        '[ "${PGDATA:-}" =', '[ /unexpected/data ='))
    assert result.returncode == 73
    assert status == "stage=data-directory exit=73\n"


def test_data_ancestors_are_prepared_before_privilege_drop():
    source = SOURCE.read_text()
    assert "chown postgres:postgres /var/lib/postgresql /var/lib/postgresql/18" in source
    assert source.index("chown postgres:postgres") < source.index("exec /usr/local/bin/docker-entrypoint.sh")
    assert "export PGPASSWORD" not in source
    assert "unset PGPASSWORD POSTGRES_PASSWORD POSTGRES_PASSWORD_FILE" in source
    assert "--auth-local=scram-sha-256" in source


def test_inherited_passwords_are_not_used_or_reported(bootstrap):
    run, _ = bootstrap
    sentinel = "SYNTHETIC_INHERITED_PASSWORD"
    result, status = run(inherited={"PGPASSWORD": sentinel, "POSTGRES_PASSWORD": sentinel,
                                  "POSTGRES_PASSWORD_FILE": "/unrelated/file", "POSTGRES_HOST_AUTH_METHOD": "trust"})
    assert result.returncode == 0
    assert sentinel not in result.stdout + result.stderr + status


def test_bootstrap_credential_permissions_and_hook_contract():
    source = SOURCE.read_text()
    assert "chmod 0444" not in source
    assert "chown postgres:18084 /run/nfc-harness /run/nfc-harness/admin_password /run/nfc-harness/run_id" in source
    assert "chmod 0750 /run/nfc-harness" in source
    assert "chmod 0440 /run/nfc-harness/admin_password /run/nfc-harness/run_id" in source
    assert source.index("umask 077") < source.index("mark\n")
    dockerfile = (SOURCE.parent / "postgres.Dockerfile").read_text()
    assert "groupadd --gid 18084 nfc-secrets" in dockerfile
    assert "test -z" in dockerfile and "find /docker-entrypoint-initdb.d" in dockerfile
    assert "chmod 0644 /docker-entrypoint-initdb.d/99-nfc-clear-init-passwords.sh" in dockerfile
    hook = (SOURCE.parent / "clear-init-passwords.sh").read_text()
    commands = [line for line in hook.splitlines() if line and not line.startswith("#")]
    assert commands == ["unset PGPASSWORD POSTGRES_PASSWORD POSTGRES_PASSWORD_FILE"]


def test_bootstrap_and_sourced_hook_shell_syntax(posix_shell):
    # Match the image's CRLF normalization; parse only, never execute commands.
    for path in (SOURCE, SOURCE.parent / "clear-init-passwords.sh"):
        result = subprocess.run([posix_shell, "-n"], input=path.read_text().encode(),
                                capture_output=True, timeout=5)
        assert result.returncode == 0
