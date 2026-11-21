"""Host orchestration tests: every Docker command is replaced by an in-memory fake."""
import copy
import json
import io
import subprocess
from unittest.mock import Mock

import pytest
import yaml

from infra.nfc_postgres import docker_runner as runner


def test_execute_decodes_both_pipes_as_utf8_with_replacement(monkeypatch):
    """Exercise actual stream decoding without launching a process or Docker."""
    def popen(args, **kwargs):
        assert kwargs['text'] is True
        assert kwargs['encoding'] == 'utf-8'
        assert kwargs['errors'] == 'replace'
        def communicate(timeout):
            assert timeout == 5
            return tuple(io.TextIOWrapper(io.BytesIO(data),
                encoding=kwargs['encoding'], errors=kwargs['errors']).read()
                for data in (b'caf\xc3\xa9 \x81\n', b'\xe2\x9c\x93 \xff\n'))
        return Mock(communicate=communicate, returncode=0)
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    result = runner.execute(['fake-command'], {}, 5)
    assert result.returncode == 0
    assert result.stdout == 'caf\u00e9 \ufffd\n'
    assert result.stderr == '\u2713 \ufffd\n'


class Docker:
    def __init__(self, fail=None, timeout=False, tamper=None):
        self.created = self.cleaned = False
        self.calls = []
        self.fail, self.timeout, self.tamper = fail, timeout, tamper

    def __call__(self, args, env, timeout):
        self.calls.append((args, env, timeout))
        out, code = '', 0
        if args[1:3] == ['context', 'show']:
            return subprocess.CompletedProcess(args, 0, 'desktop-linux\n', '')
        assert args[1:3] == ['--context', 'desktop-linux']
        a = args[3:]
        if a[0] == 'info':
            out = 'linux'
        elif a[0] == 'compose':
            self.project = a[a.index('-p') + 1]
            action = next(x for x in ('config', 'build', 'create', 'up', 'down') if x in a)
            if action == 'create': self.created = True
            if action == 'down': self.cleaned = True
            if action == 'up':
                out = 'worker | ' + json.dumps(dict(passed=3, failed=0, skipped=0, exit_code=0, failures=[]))
            if action == self.fail:
                if self.timeout: raise runner.CommandTimeout('synthetic timeout')
                code = 1
        elif a[0] == 'ps':
            out = 'pg\nworker' if self.created and not self.cleaned else ''
        elif a[0] == 'inspect':
            service = 'postgres' if a[1] == 'pg' else 'worker'
            worker = service == 'worker'
            item = dict(Id=a[1], Name='/' + a[1], Config=dict(User='18085:18085' if worker else '', Labels={
                'com.docker.compose.project': self.project, 'org.inventory.harness': runner.MARKER,
                'com.docker.compose.service': service}), HostConfig=dict(Privileged=False, PortBindings={}, Binds=[],
                NetworkMode='container:pg' if worker else 'none', ReadonlyRootfs=worker), Mounts=[dict(
                Type='volume', Name=self.project+'_identity', Destination='/run/nfc-harness', RW=not worker)])
            if self.tamper == 'network' and worker: item['HostConfig']['NetworkMode'] = 'container:unrelated'
            out = json.dumps([item])
        elif a[:2] == ['volume', 'ls']:
            out = self.project+'_identity' if self.created and not self.cleaned else ''
        elif a[:2] == ['volume', 'inspect']:
            out = json.dumps([dict(Labels={'com.docker.compose.project': self.project,
                'org.inventory.harness': 'foreign' if self.tamper == 'volume' else runner.MARKER,
                'com.docker.compose.volume': 'identity'}, Driver='local',
                Options={'type': 'tmpfs', 'device': 'tmpfs', 'o': 'size=1m,mode=0755'})])
        elif a[:2] != ['network', 'ls']:
            raise AssertionError(a)
        return subprocess.CompletedProcess(args, code, out, 'sensitive diagnostic withheld')


@pytest.fixture(autouse=True)
def no_real_docker(monkeypatch):
    monkeypatch.delenv('DOCKER_HOST', raising=False)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: 'fake-docker')
    monkeypatch.setattr(runner.subprocess, 'Popen', Mock(side_effect=AssertionError('No real processes')))


def test_success_pins_context_filters_environment_and_cleans(monkeypatch, capsys):
    for key in ('PGHOST', 'COMPOSE_FILE', 'INVENTORY_APP_PASSWORD', 'STUDENT_NFC_PAYLOAD_KEY'):
        monkeypatch.setenv(key, 'sensitive')
    docker = Docker()
    assert runner.run(51, 52, 53, invoke=docker) == 0
    assert docker.cleaned
    for args, env, timeout in docker.calls:
        assert 0 < timeout <= 90
        assert not any(key in env for key in ('PGHOST', 'COMPOSE_FILE', 'INVENTORY_APP_PASSWORD', 'STUDENT_NFC_PAYLOAD_KEY'))
        assert env['NFC_DISPOSABLE_OPT_IN'] == 'YES_ISOLATED_PG18'
        if 'compose' in args: assert '--env-file' in args and '-p' in args and '-f' in args
    output = capsys.readouterr().out
    assert '"passed": 3' in output and 'removed' in output and 'sensitive' not in output


@pytest.mark.parametrize('action', ['create', 'up'])
@pytest.mark.parametrize('timeout', [False, True])
def test_failure_or_timeout_cleans_without_retry(action, timeout):
    docker = Docker(fail=action, timeout=timeout)
    if action == 'up' and not timeout:
        assert runner.run(invoke=docker) == 1
    else:
        with pytest.raises(runner.RunnerError): runner.run(invoke=docker)
    assert docker.cleaned
    assert sum(action in args for args, _, _ in docker.calls) == 1


@pytest.mark.parametrize('tamper', ['network', 'volume'])
def test_foreign_resources_fail_closed_before_start_or_cleanup(tamper):
    docker = Docker(tamper=tamper)
    with pytest.raises(runner.RunnerError): runner.run(invoke=docker)
    assert not docker.cleaned
    assert not any('up' in a for a, _, _ in docker.calls)


def test_cleanup_failure_is_not_success(capsys):
    docker = Docker(fail='down')
    with pytest.raises(runner.RunnerError): runner.run(invoke=docker)
    assert 'resources removed' not in capsys.readouterr().out


@pytest.mark.parametrize('change', ['ports', 'bind', 'host', 'environment', 'external', 'service'])
def test_static_isolation_drift_is_rejected(change):
    config = yaml.safe_load((runner.ROOT/'infra/nfc_postgres/compose.yaml').read_text())
    config = copy.deepcopy(config)
    pg = config['services']['postgres']
    if change == 'ports': pg['ports'] = ['5432:5432']
    if change == 'bind': pg['volumes'] = ['C:/protected:/var/lib/postgresql']
    if change == 'host': pg['network_mode'] = 'host'
    if change == 'environment': pg['environment']['PGHOST'] = 'host.docker.internal'
    if change == 'external': config['volumes']['identity']['external'] = True
    if change == 'service': config['services']['unexpected'] = {}
    with pytest.raises(runner.RunnerError): runner.validate_compose(config)


def test_unavailable_docker_never_falls_back(monkeypatch):
    monkeypatch.setattr(runner.shutil, 'which', lambda _: None)
    invoke = Mock()
    with pytest.raises(runner.RunnerError): runner.run(invoke=invoke)
    invoke.assert_not_called()


def test_docker_host_override_is_rejected(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'tcp://untrusted:2375')
    invoke = Mock()
    with pytest.raises(runner.RunnerError): runner.run(invoke=invoke)
    invoke.assert_not_called()


def test_build_failure_never_starts_database():
    docker = Docker(fail='build')
    with pytest.raises(runner.RunnerError): runner.run(invoke=docker)
    assert not docker.created and not docker.cleaned


def test_windows_timeout_terminates_only_own_process_tree(monkeypatch):
    process = Mock(pid=123, returncode=1)
    process.communicate.side_effect = [subprocess.TimeoutExpired('fake', 2), ('', '')]
    monkeypatch.setattr(runner.subprocess, 'Popen', Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(runner.subprocess, 'run', kill)
    if runner.os.name != 'nt': pytest.skip('Windows process-tree termination')
    with pytest.raises(runner.CommandTimeout): runner.execute(['fake'], {}, 2)
    kill.assert_called_once_with(['taskkill.exe', '/PID', '123', '/T', '/F'], capture_output=True, timeout=10, check=False)
    process.kill.assert_called_once()


def test_wrapper_requires_bounded_runner():
    source = (runner.ROOT/'scripts/run_disposable_docker_tests.ps1').read_text()
    assert '-m infra.nfc_postgres.docker_runner' in source
    assert all(name in source for name in ('BuildTimeoutSeconds', 'TestTimeoutSeconds', 'CleanupTimeoutSeconds'))


def test_handover_module_dependencies_import_without_database_or_docker():
    from importlib import import_module
    module = import_module('infra.nfc_postgres.tests.test_handover')
    assert callable(module.test_populated_merge_head_upgrade_preserves_borrower_checkout_and_pending_nfc)
    assert callable(module.test_runtime_maintenance_lifecycle_and_reporting)
    from infra.nfc_postgres.diagnostics import SAFE_FILES
    assert 'infra/nfc_postgres/tests/test_handover.py' in SAFE_FILES
