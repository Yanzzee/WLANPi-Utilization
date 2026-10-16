import os
import shlex
import subprocess
import sys
import venv
import json
from pathlib import Path

import pytest

from scripts import install_app_venv

ROOT = Path(__file__).parents[1]
SETUP = ROOT / "scripts/pi_setup.sh"
INSTALL = ROOT / "scripts/install_app_venv.py"


@pytest.fixture
def isolated_env(tmp_path):
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    return environment


def setup_environment(tmp_path, environment=None):
    variables = dict(os.environ)
    variables.pop("VIRTUAL_ENV", None)
    variables.pop("PYTHONPATH", None)
    variables["PYTHONNOUSERSITE"] = "1"
    if environment:
        variables["VIRTUAL_ENV"] = str(environment)
    # Model hardware commands without changing the host's PATH or services.
    fake_bin = tmp_path / "hardware"
    fake_bin.mkdir()
    for command in ("ip", "iw", "tshark", "dumpcap", "git"):
        path = fake_bin / command
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    variables["PATH"] = f"{fake_bin}:{variables['PATH']}"
    return variables


def instrument_python(environment, tmp_path):
    """Keep real venv preflight/replay, intercept packaging and recursive pytest."""
    python = environment / "bin/python"
    python.unlink()
    executable = shlex.quote(str(environment / "bin/python3"))
    python.write_text(f'''#!/bin/sh
printf '%s\\n' "$*" >> "$UTILIZATION_SETUP_CALLS"
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  if [ "$3" = "--version" ]; then
    [ "$UTILIZATION_SETUP_FAIL" = "missing-pip" ] && exit 1
    echo "pip 24.3 (test)"
    exit 0
  fi
  [ "$UTILIZATION_SETUP_FAIL" = "upgrade" ] && exit 1
  [ "$UTILIZATION_SETUP_FAIL" = "editable" ] && [ "$3" = "install" ] && [ "$4" = "-e" ] && exit 1
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "ensurepip" ]; then
  [ "$UTILIZATION_SETUP_FAIL" = "missing-pip" ] && exit 1
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  exit 0
fi
if [ "$UTILIZATION_SETUP_FAIL" = "old-python" ] && [ "$1" = "-" ]; then
  exec {executable} -c 'import sys; sys.version_info=(3,12,9); sys.version="3.12.9 (test)"; exec(sys.stdin.read())'
fi
exec {executable} "$@"
''')
    python.chmod(0o755)
    return tmp_path / "python-calls.txt"


def test_setup_without_active_venv_stops_before_packaging(tmp_path):
    variables = setup_environment(tmp_path)
    result = subprocess.run(["bash", str(SETUP)], env=variables, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Activate a Python 3.13+ virtual environment" in result.stdout
    assert "Installing" not in result.stdout


def test_fake_virtual_env_pointing_to_system_python_is_rejected(tmp_path):
    environment = tmp_path / "fake"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").symlink_to(Path(sys.executable).resolve())
    result = subprocess.run(
        ["bash", str(SETUP)], env=setup_environment(tmp_path, environment),
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "system Python will not be modified" in result.stderr
    assert "Installing" not in result.stdout


def test_setup_genuine_venv_runs_editable_install_help_replay_and_tests(tmp_path, isolated_env):
    calls = instrument_python(isolated_env, tmp_path)
    variables = setup_environment(tmp_path, isolated_env)
    variables["UTILIZATION_SETUP_CALLS"] = str(calls)
    result = subprocess.run(["bash", str(SETUP)], env=variables, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    commands = calls.read_text().splitlines()
    assert "-m pip install -e .[dev]" in commands
    assert "-m beacon_live.cli --help" in commands
    assert "-m beacon_live.device --help" in commands
    assert "-m beacon_live.cli replay --input samples/tshark_qbss_sample.tsv" in commands
    assert "-m pytest" in commands
    assert "Pi setup checks completed" in result.stdout


@pytest.mark.parametrize("failure", ["old-python", "missing-pip", "upgrade", "editable"])
def test_setup_failed_preflight_or_install_never_falls_back_to_system(tmp_path, isolated_env, failure):
    calls = instrument_python(isolated_env, tmp_path)
    variables = setup_environment(tmp_path, isolated_env)
    variables.update(UTILIZATION_SETUP_CALLS=str(calls), UTILIZATION_SETUP_FAIL=failure)
    result = subprocess.run(["bash", str(SETUP)], env=variables, capture_output=True, text=True)
    assert result.returncode != 0
    commands = calls.read_text().splitlines()
    assert "-m pytest" not in commands
    assert "-m pip install .[dev]" not in commands
    if failure == "old-python":
        assert "Python 3.13+ is required; found 3.12.9" in result.stderr
        assert not any(command.startswith("-m pip") for command in commands)


def test_setup_rejects_inherited_system_packages(tmp_path, isolated_env):
    config = isolated_env / "pyvenv.cfg"
    config.write_text(config.read_text().replace("include-system-site-packages = false", "include-system-site-packages = true"))
    result = subprocess.run(
        ["bash", str(SETUP)], env=setup_environment(tmp_path, isolated_env),
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "without system site-packages" in result.stderr
    assert "Installing" not in result.stdout


def test_production_copy_rejects_system_python_before_creating_site_packages(tmp_path, monkeypatch):
    prefix = tmp_path / "system"
    monkeypatch.setattr(install_app_venv.sys, "prefix", str(prefix))
    monkeypatch.setattr(install_app_venv.sys, "base_prefix", str(prefix))
    monkeypatch.setattr(install_app_venv.site, "getsitepackages", lambda: [str(prefix / "site-packages")])
    with pytest.raises(SystemExit, match="system Python will not be modified"):
        install_app_venv._venv_site_packages()
    assert not prefix.exists()


def test_production_copy_rejects_old_python_before_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(install_app_venv.sys, "version_info", (3, 12, 9))
    with pytest.raises(SystemExit, match="Python 3.13"):
        install_app_venv._venv_site_packages()


def test_production_site_packages_must_be_inside_venv_not_prefix_sibling(tmp_path, isolated_env, monkeypatch):
    outside = tmp_path / "environment-shadow/site-packages"
    monkeypatch.setattr(install_app_venv.sys, "prefix", str(isolated_env))
    monkeypatch.setattr(install_app_venv.site, "getsitepackages", lambda: [str(outside)])
    with pytest.raises(SystemExit, match="Could not find site-packages"):
        install_app_venv._venv_site_packages()
    assert not outside.exists()


def test_production_copy_rejects_inherited_system_packages(tmp_path, isolated_env, monkeypatch):
    (isolated_env / "pyvenv.cfg").write_text("include-system-site-packages=true\n")
    monkeypatch.setattr(install_app_venv.sys, "prefix", str(isolated_env))
    with pytest.raises(SystemExit, match="without system site-packages"):
        install_app_venv._venv_site_packages()


@pytest.mark.parametrize("extra", ["core", "web"])
def test_optional_dependencies_stay_isolated_and_can_use_offline_wheels(tmp_path, isolated_env, monkeypatch, extra):
    monkeypatch.setattr(install_app_venv.sys, 'prefix', str(isolated_env))
    monkeypatch.setattr(install_app_venv.site, 'getsitepackages',
                        lambda: [str(isolated_env / 'lib/python3.13/site-packages')])
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    commands = []
    install_app_venv._install_runtime_extra(ROOT, extra=extra, wheel_directory=wheels,
        command_runner=lambda command, **kw: commands.append((command, kw)))
    command, kwargs = commands[0]
    assert command[:5] == [sys.executable, '-I', '-m', 'pip', '--isolated']
    assert '--only-binary=:all:' in command and '--no-index' in command
    assert command[command.index('--find-links') + 1] == str(wheels)
    assert kwargs == {'check': True}
    import tomllib
    declared = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['optional-dependencies'][extra]
    assert command[-len(declared):] == declared


@pytest.mark.parametrize("extra", ["core", "web"])
def test_dependency_failure_preserves_the_existing_app(tmp_path, isolated_env, extra):
    site_packages = next(isolated_env.glob('lib/python*/site-packages'))
    package = site_packages / 'beacon_live'
    package.mkdir()
    marker = package / 'previous.py'
    marker.write_text('# previous working app\n')
    result = subprocess.run([str(isolated_env / 'bin/python'), str(INSTALL),
        '--source-root', str(ROOT), '--with-' + extra, '--wheel-dir', str(tmp_path / 'missing')],
        capture_output=True, text=True)
    assert result.returncode != 0 and 'wheel directory not found' in result.stderr
    assert marker.exists()


def test_core_client_refuses_system_python_before_pip(monkeypatch, tmp_path):
    monkeypatch.setattr(install_app_venv.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(install_app_venv.sys, 'base_prefix', str(tmp_path))
    with pytest.raises(SystemExit, match='system Python will not be modified'):
        install_app_venv._install_runtime_extra(ROOT, extra="core", command_runner=lambda *a, **kw: pytest.fail('No pip'))


def test_fresh_production_install_launchers_replay_and_repeat_install(tmp_path, isolated_env):
    variables = dict(os.environ, PYTHONPATH="", PYTHONNOUSERSITE="1")
    command = [str(isolated_env / "bin/python"), str(INSTALL), "--source-root", str(ROOT)]
    installed = subprocess.run(command, cwd=tmp_path, env=variables, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    for launcher in ("utilization", "wlanpi-utilization", "beacon-live", "wlanpi-beacon-live"):
        help_result = subprocess.run(
            [str(isolated_env / "bin" / launcher), "--help"], cwd=tmp_path,
            env=variables, capture_output=True, text=True,
        )
        assert help_result.returncode == 0, help_result.stderr
        imported = subprocess.run([str(isolated_env / 'bin/python'), '-I', '-c',
            "import runpy, sys; sys.argv=['capture', '--invalid-argument']; "
            f"runpy.run_path({str(isolated_env / 'bin' / launcher)!r}, "
            "run_name='__mp_main__'); print('spawn import safe')"],
            cwd=tmp_path, env=variables, capture_output=True, text=True)
        assert imported.returncode == 0 and 'spawn import safe' in imported.stdout, imported.stderr
    replay = subprocess.run(
        [str(isolated_env / "bin/beacon-live"), "replay", "--input", str(ROOT / "samples/tshark_qbss_sample.tsv")],
        cwd=tmp_path, env=variables, capture_output=True, text=True,
    )
    assert replay.returncode == 0, replay.stderr
    assert "duration_seconds: 1.15" in replay.stdout
    package = next(isolated_env.glob("lib/python*/site-packages/beacon_live"))
    for asset in ("templates/utilization.html", "web_static/utilization.css", "web_static/utilization.js"):
        assert (package / asset).read_bytes() == (ROOT / "beacon_live" / asset).read_bytes()
    web_help = subprocess.run([str(isolated_env / "bin/wlanpi-utilization"), "web", "--help"],
        cwd=tmp_path, env=variables, capture_output=True, text=True)
    assert web_help.returncode == 0 and "--url-prefix" in web_help.stdout
    stale = package / "old_module.py"
    stale.write_text("# obsolete installation\n")
    repeated = subprocess.run(command, cwd=tmp_path, env=variables, capture_output=True, text=True)
    assert repeated.returncode == 0, repeated.stderr
    assert not stale.exists()
