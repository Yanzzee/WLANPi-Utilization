"""Exercise the real shell coordinator with temporary Pi paths and command stubs.

Sub-installers have separate validation/rollback tests. These checks verify
default inclusion, ordering and failure propagation without root or services.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def installer(tmp_path):
    root = Path(__file__).parents[1]
    scripts = tmp_path / "repo/scripts"
    scripts.mkdir(parents=True)
    app = tmp_path / "app"
    (app / "bin").mkdir(parents=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls.jsonl"
    settings = tmp_path / "fpms.json"
    fpms = tmp_path / "fpms"
    fpms.write_text("#!/bin/sh\nexit 0\n")
    fpms.chmod(0o755)
    source = (root / "scripts/install_wlanpi_fpms.sh").read_text()
    # Only the privilege guard and absolute deployment paths are substituted.
    source = source.replace('if [[ "${EUID}" -ne 0 ]]; then', "if false; then")
    for old, new in {
        "/opt/wlanpi-beacon-live": app,
        "/usr/sbin/fpms": fpms,
        "/etc/wlanpi-beacon-live/fpms.json": settings,
        "/run/wlanpi-beacon-live": tmp_path / "run",
        "/var/log/wlanpi-beacon-live": tmp_path / "logs",
        "/usr/local/bin": tmp_path / "launchers",
    }.items():
        source = source.replace(old, str(new))
    entry = scripts / "install_wlanpi_fpms.sh"
    entry.write_text(source)
    stub = f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['INSTALL_CALLS'], 'a') as stream:
    stream.write(json.dumps({'tool': Path(sys.argv[0]).name, 'args': args}) + '\\n')
if args == ['-']:
    os.execv(sys.executable, [sys.executable, '-'])
if args and args[0].endswith('install_web_integration.py'):
    stage = 'check' if '--check' in args else 'apply'
    if os.environ.get('FAIL_WEB') == stage:
        print('Fixture web integration failure: ' + stage, file=sys.stderr)
        raise SystemExit(42)
'''
    for path in (commands / "python3", commands / "bash", app / "bin/python"):
        path.write_text(stub)
        path.chmod(0o755)

    def run(*flags, saved=None, fail_web=""):
        if saved is not None:
            settings.write_text(json.dumps(saved))
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                   INSTALL_CALLS=str(log), FAIL_WEB=fail_web)
        result = subprocess.run(["/bin/bash", str(entry), *flags], env=env,
                                capture_output=True, text=True, timeout=10)
        calls = [json.loads(row) for row in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run


def script_calls(calls, name):
    return [call["args"] for call in calls if call["args"] and Path(call["args"][0]).name == name]


@pytest.mark.parametrize("flags,saved", [
    ([], None), (["--with-web"], None), ([], {"runtime_mode": "standalone", "capture_backend": "core"}),
    (["--fpms-runtime", "managed", "--fpms-backend", "direct", "--wheel-dir", "/tmp/wheels"], None),
])
def test_one_command_includes_web_checks_dependencies_service_and_protected_install(installer, flags, saved):
    result, calls = installer(*flags, saved=saved)
    assert result.returncode == 0, result.stderr
    web = script_calls(calls, "install_web_integration.py")
    assert len(web) == 2 and web[0][1:] == ["--check"] and web[1][1:] == []
    dependencies = script_calls(calls, "install_app_venv.py")
    assert len(dependencies) == 1 and "--with-web" in dependencies[0] and "--with-core" in dependencies[0]
    assert len(script_calls(calls, "install_utilization_service.sh")) == 1
    fpms = script_calls(calls, "patch_wlanpi_fpms.py")[0]
    assert fpms[fpms.index("--runtime-mode") + 1] == (saved or {}).get("runtime_mode", "managed")
    operations = [Path(call["args"][0]).name for call in calls if call["args"]]
    assert operations.index("install_web_integration.py") < operations.index("install_app_venv.py")
    assert operations.index("patch_wlanpi_fpms.py") < len(operations) - 1
    assert operations[-1] == "install_web_integration.py"
    assert "Utilization web integration installed" in result.stdout
    if "--wheel-dir" in flags:
        assert dependencies[0][dependencies[0].index("--wheel-dir") + 1] == "/tmp/wheels"


def test_without_web_is_an_explicit_skip_and_preserves_standalone(installer):
    result, calls = installer("--without-web", saved={"runtime_mode": "standalone"})
    assert result.returncode == 0, result.stderr
    assert not script_calls(calls, "install_web_integration.py")
    assert not script_calls(calls, "install_utilization_service.sh")
    assert "--with-web" not in script_calls(calls, "install_app_venv.py")[0]
    assert "Utilization web integration installed" not in result.stdout


@pytest.mark.parametrize("stage", ["check", "apply"])
def test_web_failure_is_visible_and_never_reports_complete_install(installer, stage):
    result, calls = installer(fail_web=stage)
    assert result.returncode == 42
    assert "Fixture web integration failure" in result.stderr
    assert "Utilization web integration installed" not in result.stdout
    if stage == "check":
        assert not script_calls(calls, "install_app_venv.py")
        assert not script_calls(calls, "patch_wlanpi_fpms.py")
        assert not script_calls(calls, "install_utilization_service.sh")
