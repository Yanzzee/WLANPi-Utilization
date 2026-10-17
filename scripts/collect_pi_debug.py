#!/usr/bin/env python3
"""Collect read-only WLAN Pi diagnostics without importing FPMS hardware code."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES, TSHARK_OPTIONAL_FRAME_FIELD_NAMES, TSHARK_REQUIRED_FRAME_FIELD_NAMES
from scripts.patch_wlanpi_fpms import FPMSInstallError, _find_fpms_package
from scripts.pi_web_preflight import inspect as inspect_web_integration


class Collector:
    def __init__(self, directory: Path, *, timeout: float = 10, runner=subprocess.run):
        self.directory = directory
        self.timeout = timeout
        self.runner = runner
        self.results: list[dict] = []

    def record(self, name: str, content: str, *, status: str = 'ok', **details):
        (self.directory / name).write_text(content, encoding='utf-8')
        self.results.append({'file': name, 'status': status, **details})

    def read(self, name: str, path: Path):
        try:
            self.record(name, path.read_text(encoding='utf-8', errors='replace'), source=str(path))
        except OSError as exc:
            self.record(name, str(exc) + '\n', status='unavailable', source=str(path))

    def command(self, name: str, command: list[str], *, timeout: float | None = None, output_filter=None):
        status = 'ok'
        try:
            result = self.runner(
                command, capture_output=True, text=True, errors='replace', check=False,
                timeout=self.timeout if timeout is None else timeout,
                env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'},
            )
            if result.returncode:
                status = 'failed'
        except subprocess.TimeoutExpired as exc:
            def decode(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
            result = subprocess.CompletedProcess(command, 124, decode(exc.stdout), decode(exc.stderr) + '\nCommand timed out\n')
            status = 'timeout'
        except OSError as exc:
            result = subprocess.CompletedProcess(command, 127, '', str(exc) + '\n')
            status = 'unavailable'
        if output_filter is not None:
            result.stdout = output_filter(result.stdout)
        self.record(
            name, f'$ {shlex.join(command)}\nExit status: {result.returncode}\n\n'
            + result.stdout + '\n' + result.stderr,
            status=status, command=command, returncode=result.returncode,
        )
        return result

    def finish(self):
        (self.directory / 'manifest.json').write_text(json.dumps(self.results, indent=2) + '\n')
        failed = [r for r in self.results if r['status'] != 'ok']
        summary = ['WLAN Pi Utilization diagnostic bundle',
                   'Read-only checks; no capture, retuning, service changes, or FPMS imports.',
                   f'{len(self.results)} checks; {len(failed)} failed/unavailable/timed out.',
                   'Start with fpms_journal.txt, fpms_service.txt, fpms_paths.json,',
                   'deployment.json, tshark_fields.json, namespaces.json, and iw_dev.txt.',
                   'Failures below are evidence; partial collection is still useful.', '']
        summary += [f"{r['status']}: {r['file']}" for r in failed]
        (self.directory / 'SUMMARY.txt').write_text('\n'.join(summary) + '\n')


def collect_fpms(bundle: Collector, launcher: Path = Path('/usr/sbin/fpms')):
    details = {'launcher': str(launcher)}
    package = None
    try:
        details['target'] = str(launcher.resolve(strict=True))
        with launcher.open(encoding='utf-8') as stream:
            details['shebang'] = stream.readline(4096).strip()
        package = _find_fpms_package(
            launcher, command_runner=lambda command, **kw: bundle.command('fpms_python_probe.txt', command),
        )
        details['package'] = str(package)
    except (FPMSInstallError, OSError, RuntimeError, UnicodeError) as exc:
        details['error'] = str(exc)
    bundle.record('fpms_paths.json', json.dumps(details, indent=2) + '\n',
                  status='ok' if package else 'unavailable')
    return package


def fingerprint(path: Path):
    try:
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    except OSError as exc:
        return {'path': str(path), 'error': str(exc)}


def collect_deployment(bundle: Collector, package: Path | None, app_python: Path, repo_root: Path):
    probe = (
        "import importlib.util, json, sys; "
        "spec=importlib.util.find_spec('beacon_live'); "
        "print(json.dumps({'executable':sys.executable,'version':sys.version,"
        "'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'package':list(spec.submodule_search_locations or []) if spec else []}))"
    )
    result = bundle.command('app_python.txt', [str(app_python), '-I', '-c', probe])
    files = []
    if result.returncode == 0:
        try:
            locations = json.loads(result.stdout)['package']
            if len(locations) != 1 or not Path(locations[0]).is_absolute():
                raise ValueError('app package location is not unique/absolute')
            installed = Path(locations[0])
            for source in sorted((repo_root / 'beacon_live').glob('*.py')):
                entry = fingerprint(installed / source.name)
                entry['matches_checkout'] = entry.get('sha256') == fingerprint(source).get('sha256') and 'error' not in entry
                files.append(entry)
        except (KeyError, TypeError, ValueError) as exc:
            files.append({'error': str(exc)})
    if package:
        for relative in ('fpms.py', 'modules/nav/buttons.py', 'modules/apps/channel_utilization.py'):
            entry = fingerprint(package / relative)
            if relative.endswith('channel_utilization.py'):
                entry['matches_checkout'] = entry.get('sha256') == fingerprint(repo_root / 'integration/wlanpi_fpms/channel_utilization.py').get('sha256') and 'error' not in entry
            files.append(entry)
    status = ('mismatch' if any(f.get('matches_checkout') is False for f in files)
              else 'unavailable' if any('error' in f for f in files) else 'ok')
    bundle.record('deployment.json', json.dumps(files, indent=2) + '\n', status=status)
    for index, path in enumerate((Path('/usr/local/bin/wlanpi-utilization'), Path('/usr/local/bin/utilization'),
                                 Path('/usr/local/bin/wlanpi-beacon-live'), Path('/usr/local/bin/beacon-live'))):
        bundle.read(f'cli_launcher_{index}.txt', path)
    bundle.command('core_client.txt', [str(app_python), '-I', '-c',
        "import websockets; print(websockets.__version__); print(websockets.__file__)"])


def collect_backend_config(bundle: Collector, path=Path('/etc/wlanpi-beacon-live/fpms.json')):
    report = {'path': str(path), 'capture_backend': 'direct', 'runtime_mode': 'standalone', 'source': 'default'}
    status = 'ok'
    try:
        settings = json.loads(path.read_text())
        backend = settings.get('capture_backend', 'direct')
        if not isinstance(backend, str) or backend not in {'direct', 'core'}:
            raise ValueError('invalid capture_backend')
        runtime_mode = settings.get('runtime_mode', 'standalone')
        if not isinstance(runtime_mode, str) or runtime_mode not in {'standalone', 'managed'}:
            raise ValueError('invalid runtime_mode')
        report.update(capture_backend=backend, runtime_mode=runtime_mode, source='file')
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as error:
        report.update(capture_backend=None, error=str(error))
        status = 'failed'
    # Retain only backend/runtime selections, not arbitrary configuration values.
    bundle.record('fpms_backend.json', json.dumps(report, indent=2) + '\n', status=status)


def collect_network(bundle: Collector):
    namespaces = bundle.command('ip_netns.txt', ['ip', 'netns', 'list'])
    names = [line.split()[0] for line in namespaces.stdout.splitlines() if line.strip()] if namespaces.returncode == 0 else []
    contexts = [(None, '')] + [(name, f'netns_{index}_') for index, name in enumerate(names)]
    bundle.record('namespaces.json', json.dumps([{'namespace': name or 'root', 'file_prefix': prefix} for name, prefix in contexts], indent=2) + '\n')
    for name, prefix in contexts:
        context = ['ip', 'netns', 'exec', name] if name else []
        for filename, command in (
            ('iw_dev.txt', ['iw', 'dev']), ('iw_phy.txt', ['iw', 'phy']),
            ('iw_reg.txt', ['iw', 'reg', 'get']), ('ip_addr.txt', ['ip', '-details', 'address', 'show']),
            ('packet_sockets.txt', ['cat', '/proc/net/packet']),
            ('packet_owners.txt', ['ss', '-0', '-a', '-p', '-n']),
            ('nm_devices.txt', ['nmcli', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status']),
        ):
            bundle.command(prefix + filename, context + command)


def collect_tshark(bundle: Collector):
    result = bundle.command('tshark_fields_probe.txt', ['tshark', '-G', 'fields'])
    wanted = TSHARK_FRAME_FIELD_NAMES + TSHARK_OPTIONAL_FRAME_FIELD_NAMES
    definitions = {}
    for line in result.stdout.splitlines():
        columns = line.split('\t')
        if len(columns) >= 4 and columns[0] == 'F' and columns[2] in wanted:
            definitions[columns[2]] = columns[3]
    missing_required = [name for name in TSHARK_REQUIRED_FRAME_FIELD_NAMES if name not in definitions]
    bundle.record('tshark_fields.json', json.dumps({
        'probe_returncode': result.returncode, 'types': definitions,
        'missing_required': missing_required,
        'unavailable': [name for name in wanted if name not in definitions],
    }, indent=2) + '\n', status='failed' if result.returncode else 'mismatch' if missing_required else 'ok')
    # Retain the command outcome but avoid archiving the entire global registry.
    (bundle.directory / 'tshark_fields_probe.txt').write_text(
        f'$ tshark -G fields\nExit status: {result.returncode}\n'
        f'Requested field definitions are in tshark_fields.json\n{result.stderr}\n')


def utilization_environment(output: str) -> str:
    """Retain effective interface overrides without archiving unrelated secrets."""
    try:
        values = {
            key: value
            for item in shlex.split(output)
            if '=' in item
            for key, value in [item.split('=', 1)]
            if key in {'WLANPI_UTILIZATION_IFACE', 'WLANPI_UTILIZATION_NETNS', 'WLANPI_UTILIZATION_CAPTURE_BACKEND'}
        }
    except ValueError as exc:
        values = {'error': str(exc)}
    return json.dumps(values, indent=2) + '\n'


def collect_running_environment(bundle: Collector, service_output: str, proc_root: Path = Path('/proc')):
    values = dict(line.split('=', 1) for line in service_output.splitlines() if '=' in line)
    pid = values.get('MainPID', '')
    report = {}
    status = 'ok'
    try:
        if not pid.isdecimal() or int(pid) <= 0:
            raise ValueError('FPMS has no reported running MainPID')
        data = (proc_root / pid / 'environ').read_bytes()
        report = {
            key: value
            for item in data.decode(errors='replace').split('\0')
            if '=' in item
            for key, value in [item.split('=', 1)]
            if key in {'WLANPI_UTILIZATION_IFACE', 'WLANPI_UTILIZATION_NETNS', 'WLANPI_UTILIZATION_CAPTURE_BACKEND'}
        }
    except (OSError, ValueError) as exc:
        report = {'error': str(exc)}
        status = 'unavailable'
    bundle.record('fpms_process_environment.json', json.dumps({'pid': pid, 'overrides': report}, indent=2) + '\n', status=status)


def collect_runtime(bundle: Collector, directory: Path = Path('/run/wlanpi-beacon-live')):
    entries = []
    try:
        for path in sorted(directory.rglob('*')):
            stat = path.stat()
            entries.append({'path': str(path), 'size': stat.st_size, 'mtime': stat.st_mtime})
    except OSError as exc:
        entries.append({'error': str(exc)})
    bundle.record('runtime_files.json', json.dumps(entries, indent=2) + '\n')
    for name in ('logging.status.json', 'logging.control.json'):
        bundle.read('runtime_' + name, directory / name)


def collect(bundle: Collector, *, repo_root: Path, since: str, launcher=Path('/usr/sbin/fpms'), app_python=Path('/opt/wlanpi-beacon-live/bin/python')):
    bundle.read('os_release.txt', Path('/etc/os-release'))
    bundle.read('wlanpi_release.txt', Path('/etc/wlanpi-release'))
    bundle.record('web_integration_preflight.json', json.dumps(inspect_web_integration(
        Path('/opt/wlanpi-webui/workingdirectory'),
        Path('/etc/wlanpi-webui/nginx/sites-enabled/wlanpi_webui.conf')), indent=2) + '\n')
    service_output = ''
    for filename, command in (
        ('uname.txt', ['uname', '-a']), ('python_version.txt', ['python3', '--version']),
        ('packages.txt', ['dpkg-query', '-W', '-f=${Package}\t${Version}\t${Status}\n',
                          'wlanpi-fpms', 'wlanpi-core', 'wlanpi-common', 'wlanpi-webui', 'nginx', 'python3', 'python3-venv', 'tshark', 'wireshark-common', 'iw', 'iproute2']),
        ('tshark_version.txt', ['tshark', '--version']), ('dumpcap_version.txt', ['dumpcap', '--version']),
        ('fpms_service.txt', ['systemctl', 'show', 'wlanpi-fpms', '--no-pager',
                              '-p', 'FragmentPath', '-p', 'DropInPaths', '-p', 'ExecStart', '-p', 'User', '-p', 'Group',
                              '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'Result', '-p', 'ExecMainStatus']),
        ('fpms_journal.txt', ['journalctl', '-u', 'wlanpi-fpms', '--since', since, '-n', '300', '--no-pager', '-o', 'short-iso']),
        ('utilization_journal.txt', ['journalctl', '-u', 'wlanpi-utilization', '--since', since, '-n', '300', '--no-pager', '-o', 'short-iso']),
        ('utilization_session.txt', [str(app_python), '-m', 'beacon_live.device', 'session', 'status', '--summary']),
        ('dmesg_tail.txt', ['journalctl', '-k', '-n', '200', '--no-pager', '-o', 'short-iso']),
        ('processes.txt', ['ps', '-eo', 'pid,ppid,user,pcpu,rss,etime,comm,args']),
        ('usb_devices.txt', ['lsusb']),
        ('usb_topology.txt', ['lsusb', '-t']),
        ('radio_locks.txt', ['lslocks']),
        ('rfkill.txt', ['rfkill', 'list']), ('disk.txt', ['df', '-h', str(repo_root), '/var/log', '/run']),
        ('memory.txt', ['free', '-m']),
        # sudo changes the uid but not checkout ownership. Trust this one known
        # checkout for these read-only commands without changing Git config.
        ('git_revision.txt', ['git', '-c', f'safe.directory={repo_root}', '-C', str(repo_root), 'rev-parse', 'HEAD']),
        ('git_status.txt', ['git', '-c', f'safe.directory={repo_root}', '-C', str(repo_root), 'status', '--short']),
    ):
        result = bundle.command(filename, command)
        if filename == 'fpms_service.txt':
            service_output = result.stdout
    package = collect_fpms(bundle, launcher)
    bundle.command(
        'fpms_environment.txt', ['systemctl', 'show', 'wlanpi-fpms', '-p', 'Environment', '--value'],
        output_filter=utilization_environment,
    )
    collect_running_environment(bundle, service_output)
    collect_backend_config(bundle)
    collect_deployment(bundle, package, app_python, repo_root)
    collect_tshark(bundle)
    collect_network(bundle)
    collect_runtime(bundle)
    bundle.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=REPO_ROOT / 'debug')
    parser.add_argument('--since', default='15 minutes ago', help='FPMS journal window (default: 15 minutes ago)')
    parser.add_argument('--timeout-seconds', type=float, default=10, help='Time limit per command')
    args = parser.parse_args()
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error('--timeout-seconds must be finite and positive')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = Path(tempfile.mkdtemp(prefix=f'pi_debug_{timestamp}_', dir=args.output_dir))
    collect(Collector(directory, timeout=args.timeout_seconds), repo_root=REPO_ROOT, since=args.since)
    archive = directory.with_suffix('.tar.gz')
    with tarfile.open(archive, 'w:gz') as stream:
        stream.add(directory, arcname=directory.name)
    print((directory / 'SUMMARY.txt').read_text())
    print(f'Debug archive written to:\n{archive}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
