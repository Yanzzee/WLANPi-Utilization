"""Read-only diagnostics survive the failure states they must help diagnose."""
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import venv

import pytest

from scripts import collect_pi_debug as debug


def test_command_failures_are_preserved_and_collection_continues(tmp_path):
    outcomes = [FileNotFoundError('no iw'), subprocess.TimeoutExpired('tshark', 1, output=b'partial rows'),
                subprocess.CompletedProcess(['journalctl'], 1, '', 'Permission denied'),
                subprocess.CompletedProcess(['uname'], 0, 'Linux test\n', '')]

    def run(command, **options):
        assert options['timeout'] == 1
        assert options['env']['LC_ALL'] == 'C'
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    bundle = debug.Collector(tmp_path, timeout=1, runner=run)
    for name in ('missing', 'timeout', 'denied', 'success'):
        bundle.command(name + '.txt', [name])
    bundle.finish()
    results = json.loads((tmp_path / 'manifest.json').read_text())
    assert [r['status'] for r in results] == ['unavailable', 'timeout', 'failed', 'ok']
    assert 'partial rows' in (tmp_path / 'timeout.txt').read_text()
    assert 'Permission denied' in (tmp_path / 'denied.txt').read_text()
    assert 'Linux test' in (tmp_path / 'success.txt').read_text()
    assert '3 failed/unavailable/timed out' in (tmp_path / 'SUMMARY.txt').read_text()


def test_namespaces_have_safe_distinct_files_and_read_only_commands(tmp_path):
    calls = []

    def run(command, **options):
        calls.append(command)
        output = 'capture-lab (id: 0)\n../../escape\n' if command == ['ip', 'netns', 'list'] else 'monitor evidence\n'
        return subprocess.CompletedProcess(command, 0, output, '')

    debug.collect_network(debug.Collector(tmp_path, runner=run))
    mapping = json.loads((tmp_path / 'namespaces.json').read_text())
    assert mapping == [{'namespace': 'root', 'file_prefix': ''},
                       {'namespace': 'capture-lab', 'file_prefix': 'netns_0_'},
                       {'namespace': '../../escape', 'file_prefix': 'netns_1_'}]
    assert ['ip', 'netns', 'exec', 'capture-lab', 'iw', 'dev'] in calls
    assert ['ip', 'netns', 'exec', '../../escape', 'cat', '/proc/net/packet'] in calls
    assert (tmp_path / 'iw_dev.txt').exists() and (tmp_path / 'netns_0_iw_dev.txt').exists()
    assert not any('set' in command or 'add' in command or 'del' in command for command in calls)


@pytest.fixture
def installed_packages(tmp_path):
    environment = tmp_path / 'environment'
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    python = environment / 'bin/python'
    purelib = Path(subprocess.check_output([str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    fpms = purelib / 'fpms'
    app = purelib / 'beacon_live'
    for package in (fpms, app):
        package.mkdir()
        (package / '__init__.py').write_text('raise RuntimeError("hardware initializer must not run")\n')
    (fpms / 'modules/apps').mkdir(parents=True)
    (fpms / 'modules/nav').mkdir(parents=True)
    entry = environment / 'bin/fpms'
    entry.write_text(f'#!{python}\nraise RuntimeError("FPMS launcher must not run")\n')
    launcher = tmp_path / 'fpms'
    launcher.symlink_to(entry)
    source = tmp_path / 'checkout'
    (source / 'beacon_live').mkdir(parents=True)
    (source / 'integration/wlanpi_fpms').mkdir(parents=True)
    (source / 'beacon_live/live.py').write_text('# current capture\n')
    (app / 'live.py').write_text('# stale capture\n')
    (source / 'integration/wlanpi_fpms/channel_utilization.py').write_text('# current adapter\n')
    (fpms / 'modules/apps/channel_utilization.py').write_text('# stale adapter\n')
    return launcher, python, fpms, source


def test_active_package_discovery_and_stale_deployment_without_hardware_import(tmp_path, installed_packages):
    launcher, python, package, checkout = installed_packages
    output = tmp_path / 'bundle'
    output.mkdir()
    bundle = debug.Collector(output)
    assert debug.collect_fpms(bundle, launcher) == package.resolve()
    debug.collect_deployment(bundle, package, python, checkout)
    paths = json.loads((output / 'fpms_paths.json').read_text())
    assert paths['package'] == str(package.resolve())
    assert str(python) in paths['shebang']
    files = json.loads((output / 'deployment.json').read_text())
    mismatches = [Path(f['path']).name for f in files if f.get('matches_checkout') is False]
    assert mismatches == ['live.py', 'channel_utilization.py']
    runtime = json.loads((output / 'app_python.txt').read_text().split('\n\n', 1)[1])
    assert runtime['prefix'] != runtime['base_prefix']


def test_field_report_keeps_requested_types_and_probe_error(tmp_path):
    fields = 'P\tIEEE\twlan\nF\tSSID\twlan.ssid\tFT_BYTES\nF\tOther\tunused.field\tFT_UINT8\n'
    runner = lambda command, **kw: subprocess.CompletedProcess(command, 1, fields, 'plugin failure')
    bundle = debug.Collector(tmp_path, runner=runner)
    debug.collect_tshark(bundle)
    report = json.loads((tmp_path / 'tshark_fields.json').read_text())
    assert report['types'] == {'wlan.ssid': 'FT_BYTES'}
    assert 'wlan.fc.retry' in report['unavailable']
    assert report['probe_returncode'] == 1
    assert 'plugin failure' in (tmp_path / 'tshark_fields_probe.txt').read_text()
    assert 'unused.field' not in (tmp_path / 'tshark_fields_probe.txt').read_text()


def test_failed_fpms_startup_bundle_contains_journal_and_manifest(tmp_path, monkeypatch):
    calls = []

    def run(command, **options):
        calls.append(command)
        if command[0] == 'journalctl' and '-u' in command:
            output = 'Capture Stopped (2): Radio phy0 is busy: tcpdump (pid 42)\n'
        elif command[0] == 'systemctl':
            output = 'ActiveState=failed\nExecMainStatus=2\n'
        else:
            raise FileNotFoundError(command[0])
        return subprocess.CompletedProcess(command, 0, output, '')

    original_collect = debug.collect
    # Keep the real collection flow, with fake commands and isolated paths.
    def isolated_collect(bundle, **options):
        bundle.runner = run
        original_collect(bundle, repo_root=tmp_path, since=options['since'],
                         launcher=tmp_path / 'missing-fpms', app_python=tmp_path / 'missing-python')

    monkeypatch.setattr(debug, 'collect', isolated_collect)
    monkeypatch.setattr(sys, 'argv', ['collect', '--output-dir', str(tmp_path), '--since', '5 minutes ago'])
    assert debug.main() == 0
    archive = next(tmp_path.glob('pi_debug_*.tar.gz'))
    with tarfile.open(archive) as stream:
        members = {Path(m.name).name: m for m in stream.getmembers() if m.isfile()}
        assert b'tcpdump (pid 42)' in stream.extractfile(members['fpms_journal.txt']).read()
        assert b'ActiveState=failed' in stream.extractfile(members['fpms_service.txt']).read()
        assert b'"unavailable"' in stream.extractfile(members['manifest.json']).read()
        assert 'fpms_paths.json' in members and 'namespaces.json' in members
    assert ['journalctl', '-u', 'wlanpi-fpms', '--since', '5 minutes ago', '-n', '300', '--no-pager', '-o', 'short-iso'] in calls
    assert not any('restart' in c or 'stop' in c or 'set' in c or '-i' in c for c in calls)


def test_shell_wrapper_passes_options_without_development_packages():
    wrapper = Path(__file__).parents[1] / 'scripts/pi_collect_debug.sh'
    result = subprocess.run(['bash', str(wrapper), '--help'], capture_output=True, text=True, check=True)
    assert '--since' in result.stdout and '--output-dir' in result.stdout


def test_only_effective_utilization_overrides_are_archived(tmp_path):
    raw = '"WLANPI_UTILIZATION_IFACE=renamed" "WLANPI_UTILIZATION_NETNS=lab" CORE_API_TOKEN=secret-value'
    bundle = debug.Collector(tmp_path, runner=lambda command, **kw: subprocess.CompletedProcess(command, 0, raw, ''))
    result = bundle.command('environment.txt', ['systemctl'], output_filter=debug.utilization_environment)
    assert json.loads(result.stdout) == {'WLANPI_UTILIZATION_IFACE': 'renamed', 'WLANPI_UTILIZATION_NETNS': 'lab'}
    assert 'secret-value' not in (tmp_path / 'environment.txt').read_text()


def test_running_fpms_environment_includes_environment_file_overrides_only(tmp_path):
    proc = tmp_path / 'proc' / '123'
    proc.mkdir(parents=True)
    (proc / 'environ').write_bytes(b'CORE_TOKEN=secret\0WLANPI_UTILIZATION_IFACE=wlanpi0\0WLANPI_UTILIZATION_NETNS=lab\0')
    bundle = debug.Collector(tmp_path)
    debug.collect_running_environment(bundle, 'MainPID=123\nActiveState=active\n', tmp_path / 'proc')
    text = (tmp_path / 'fpms_process_environment.json').read_text()
    assert 'secret' not in text
    assert json.loads(text)['overrides'] == {'WLANPI_UTILIZATION_IFACE': 'wlanpi0', 'WLANPI_UTILIZATION_NETNS': 'lab'}


def test_missing_fpms_process_environment_is_recorded_not_fatal(tmp_path):
    bundle = debug.Collector(tmp_path)
    debug.collect_running_environment(bundle, 'MainPID=0\n', tmp_path / 'proc')
    assert bundle.results[-1]['status'] == 'unavailable'
    assert 'no reported running MainPID' in (tmp_path / 'fpms_process_environment.json').read_text()


@pytest.mark.parametrize('value', ['0', 'nan', 'inf'])
def test_unbounded_or_invalid_timeout_is_rejected(value, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['collect', '--output-dir', str(tmp_path), '--timeout-seconds', value])
    with pytest.raises(SystemExit) as error:
        debug.main()
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())


def test_successful_probe_with_missing_required_fields_is_flagged(tmp_path):
    runner = lambda command, **kw: subprocess.CompletedProcess(command, 0, 'F\tSSID\twlan.ssid\tFT_BYTES\n', '')
    bundle = debug.Collector(tmp_path, runner=runner)
    debug.collect_tshark(bundle)
    report = json.loads((tmp_path / 'tshark_fields.json').read_text())
    assert 'wlan.fc.retry' in report['missing_required']
    assert bundle.results[-1]['status'] == 'mismatch'
