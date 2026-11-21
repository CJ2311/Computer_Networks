"""Host-side disposable runner. Importing this module executes no commands."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
MARKER = "nfc-disposable"


class RunnerError(RuntimeError):
    pass


class CommandTimeout(RunnerError):
    pass


def execute(args, env, timeout):
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", **options)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill this command's tree, not just Docker's parent CLI: a surviving
        # Compose plugin must not race finally-cleanup and recreate resources.
        try:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            process.kill()
            process.communicate(timeout=10)
        raise CommandTimeout("Docker command exceeded its execution budget") from None
    return subprocess.CompletedProcess(args, process.returncode, out, err)


def validate_compose(config):
    """Fail closed before Docker on changes to the reviewed isolation contract."""
    def require(condition):
        if not condition:
            raise RunnerError("Compose isolation contract changed; review before execution")
    try:
        require(set(config) == {"services", "volumes"})
        require(set(config["services"]) == {"postgres", "worker"})
        pg, worker = config["services"]["postgres"], config["services"]["worker"]
        require(set(pg) == {"build", "environment", "network_mode", "volumes", "tmpfs", "shm_size", "healthcheck", "logging", "labels"})
        require(set(worker) == {"build", "network_mode", "user", "group_add", "depends_on", "environment", "volumes", "read_only", "tmpfs", "cap_drop", "security_opt", "labels"})
        require(pg["network_mode"] == "none" and worker["network_mode"] == "service:postgres")
        require(pg["volumes"] == ["identity:/run/nfc-harness"])
        require(worker["volumes"] == ["identity:/run/nfc-harness:ro"])
        require(pg["tmpfs"] == ["/var/lib/postgresql:size=768m,mode=0700"])
        require(worker["tmpfs"] == ["/tmp:size=128m,mode=1777"])
        require(pg["logging"] == {"driver": "none"})
        require(worker["read_only"] is True and worker["user"] == "18085:18085")
        require(worker["group_add"] == ["18084"] and worker["cap_drop"] == ["ALL"])
        require(worker["security_opt"] == ["no-new-privileges:true"])
        opt_in = "${NFC_DISPOSABLE_OPT_IN:?Explicit disposable-test opt-in required}"
        require(pg["environment"] == {"NFC_DISPOSABLE_OPT_IN": opt_in})
        require(worker["environment"] == {"NFC_DISPOSABLE_OPT_IN": opt_in,
            "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
        for name, service in config["services"].items():
            require(service["labels"] == {"org.inventory.harness": MARKER})
            require(service["build"] == {"context": "../..", "dockerfile": f"infra/nfc_postgres/{name}.Dockerfile"})
        require(config["volumes"] == {"identity": {"driver": "local", "driver_opts": {
            "type": "tmpfs", "device": "tmpfs", "o": "size=1m,mode=0755"}, "labels": {"org.inventory.harness": MARKER}}})
    except (KeyError, TypeError):
        raise RunnerError("Compose isolation contract changed; review before execution") from None


def run(build_timeout=900, test_timeout=1800, cleanup_timeout=120, *, invoke=execute):
    config = yaml.safe_load((ROOT / "infra/nfc_postgres/compose.yaml").read_text())
    validate_compose(config)
    if os.environ.get("DOCKER_HOST"):
        raise RunnerError("Unset DOCKER_HOST and select a trusted Docker context explicitly")
    # Do not forward backend, identity-provider or libpq settings to Compose.
    env = {key: value for key, value in os.environ.items() if not key.startswith((
        "COMPOSE_", "PG", "INVENTORY_", "AUTH_", "COLLEGE_", "STUDENT_", "SMTP_", "OVERDUE_"))}
    env["NFC_DISPOSABLE_OPT_IN"] = "YES_ISOLATED_PG18"
    docker = shutil.which("docker")
    if not docker:
        raise RunnerError("Docker CLI unavailable; no PostgreSQL fallback is permitted")
    result = invoke([docker, "context", "show"], env, 30)
    context = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[A-Za-z0-9_.-]+", context):
        raise RunnerError("Trusted Docker context unavailable")
    project = "nfc-it-" + uuid.uuid4().hex
    print(f"Disposable project={project} context={context}", flush=True)

    def command(arguments, timeout=30, check=True):
        result = invoke([docker, "--context", context, *arguments], env, timeout)
        if check and result.returncode:
            raise RunnerError("Docker operation failed; details withheld")
        return result

    compose = ["compose", "--env-file", str(ROOT / "infra/nfc_postgres/compose.defaults"),
               "-p", project, "-f", str(ROOT / "infra/nfc_postgres/compose.yaml")]

    def resources(require_complete=False):
        ids = command(["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).stdout.split()
        services = set()
        containers = {}
        for identifier in ids:
            item = json.loads(command(["inspect", identifier]).stdout)[0]
            labels = item["Config"]["Labels"]
            service = labels.get("com.docker.compose.service")
            if (labels.get("com.docker.compose.project") != project or labels.get("org.inventory.harness") != MARKER
                    or service not in {"postgres", "worker"} or service in services):
                raise RunnerError("Abort cleanup: unexpected container ownership")
            services.add(service)
            containers[service] = item
            host = item["HostConfig"]
            if host.get("Privileged") or host.get("PortBindings") or host.get("Binds") and any(
                    not bind.startswith(project + "_identity:/run/nfc-harness:") for bind in host["Binds"]):
                raise RunnerError("Abort cleanup: unexpected container exposure")
            if service == "postgres" and host["NetworkMode"] != "none":
                raise RunnerError("Abort cleanup: PostgreSQL network isolation changed")
            if service == "worker" and not host["NetworkMode"].startswith("container:"):
                raise RunnerError("Abort cleanup: worker network isolation changed")
            if service == "worker" and (not host.get("ReadonlyRootfs") or item["Config"].get("User") != "18085:18085"):
                raise RunnerError("Abort cleanup: worker isolation changed")
            for mount in item.get("Mounts", []):
                if not ((mount["Type"] == "volume" and mount.get("Name") == project + "_identity"
                         and mount["Destination"] == "/run/nfc-harness") or
                        (mount["Type"] == "tmpfs" and mount["Destination"] ==
                         ("/tmp" if service == "worker" else "/var/lib/postgresql"))):
                    raise RunnerError("Abort cleanup: unexpected container mount")
                if service == "worker" and mount["Type"] == "volume" and mount.get("RW") is not False:
                    raise RunnerError("Abort cleanup: writable worker credential volume")
        if services == {"postgres", "worker"}:
            pg = containers["postgres"]
            shared = containers["worker"]["HostConfig"]["NetworkMode"][len("container:"):]
            if shared not in {pg["Id"], pg["Name"].lstrip("/")}:
                raise RunnerError("Abort cleanup: worker shares an unrelated container namespace")
        volumes = command(["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]).stdout.split()
        for name in volumes:
            item = json.loads(command(["volume", "inspect", name]).stdout)[0]
            labels = item["Labels"]
            if (name != project + "_identity" or labels.get("com.docker.compose.project") != project
                    or labels.get("org.inventory.harness") != MARKER or labels.get("com.docker.compose.volume") != "identity"
                    or item["Driver"] != "local" or item["Options"] != config["volumes"]["identity"]["driver_opts"]):
                raise RunnerError("Abort cleanup: unexpected volume ownership")
        if command(["network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]).stdout.strip():
            raise RunnerError("Abort cleanup: unexpected project network")
        if require_complete and (services != {"postgres", "worker"} or volumes != [project + "_identity"]):
            raise RunnerError("Disposable resources incomplete")
        return ids, volumes

    if command(["info", "--format", "{{.OSType}}" ]).stdout.strip() != "linux":
        raise RunnerError("Docker Linux engine required; no PostgreSQL fallback is permitted")
    command([*compose, "config", "--quiet"])
    command([*compose, "build"], build_timeout)
    try:
        # Inspect stopped containers BEFORE any database process or worker starts.
        command([*compose, "create"], 90)
        resources(require_complete=True)
        result = command([*compose, "up", "--no-build", "--abort-on-container-exit", "--exit-code-from", "worker"],
                         test_timeout, check=False)
        records = []
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line[line.index("{"):])
                if {"exit_code", "failures"} <= record.keys():
                    records.append(record)
            except (ValueError, TypeError, AttributeError):
                continue
        if len(records) != 1:
            raise RunnerError("Missing unambiguous sanitized worker results")
        record = records[0]
        print(json.dumps(record), flush=True)
        if not record.get("passed") or record.get("skipped") or record.get("failed") or record["exit_code"]:
            return result.returncode or 1
        return result.returncode
    finally:
        # A failed create/up/timeout still gets checked, project-scoped cleanup.
        resources()
        command([*compose, "down", "--volumes", "--timeout", "30"], cleanup_timeout)
        if any(resources()):
            raise RunnerError("Disposable resources remain after cleanup")
        print("Disposable containers and identity volume removed.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in (("build-timeout", 900), ("test-timeout", 1800), ("cleanup-timeout", 120)):
        parser.add_argument("--" + name, type=int, default=default)
    args = parser.parse_args(argv)
    if any(not 1 <= value <= 86400 for value in vars(args).values()):
        parser.error("Timeouts must be between 1 and 86400 seconds")
    try:
        return run(**vars(args))
    except CommandTimeout:
        print("Disposable verification timed out; no retry. Review saved project/context if cleanup failed.")
        return 124
    except (Exception, KeyboardInterrupt):
        print("Disposable verification stopped; details withheld. Review saved project/context before retrying.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
