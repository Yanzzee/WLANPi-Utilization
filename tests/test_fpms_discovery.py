import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from scripts.patch_wlanpi_fpms import FPMSInstallError
from scripts.patch_wlanpi_fpms import _find_fpms_package
from scripts.patch_wlanpi_fpms import install_fpms


FIXTURES = Path(__file__).parent / "fixtures" / "fpms_1_4_24"
ADAPTER = (
    Path(__file__).parents[1]
    / "integration" / "wlanpi_fpms" / "channel_utilization.py"
)


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path, Path]:
    environment = tmp_path / "opt" / "wlanpi-fpms"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    interpreter = environment / "bin" / "python"
    site_packages = Path(subprocess.check_output(
        [str(interpreter), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
    ).strip())
    package = site_packages / "fpms"
    (package / "modules" / "nav").mkdir(parents=True)
    (package / "modules" / "apps").mkdir()
    # Looking up a top-level package must not execute its initializer.
    (package / "__init__.py").write_text("raise RuntimeError('FPMS must not be imported')\n")
    shutil.copy2(FIXTURES / "fpms.py", package / "fpms.py")
    shutil.copy2(FIXTURES / "buttons.py", package / "modules" / "nav" / "buttons.py")
    entrypoint = environment / "bin" / "fpms"
    entrypoint.write_text(
        f"#!{interpreter}\nfrom fpms.__main__ import main\nraise SystemExit(main())\n"
    )
    entrypoint.chmod(0o755)
    launcher = tmp_path / "usr" / "sbin" / "fpms"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(entrypoint)
    return launcher, interpreter, package


def test_discovery_uses_venv_and_ignores_stale_and_shadow_packages(
    installation: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, interpreter, package = installation
    stale = interpreter.parent.parent / "lib" / "python3.11" / "site-packages" / "fpms"
    stale.mkdir(parents=True)
    (stale / "fpms.py").write_text("# stale package\n")
    shadow = tmp_path / "shadow"
    (shadow / "fpms").mkdir(parents=True)
    (shadow / "fpms" / "__init__.py").write_text("raise RuntimeError('shadow package')\n")
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    monkeypatch.chdir(shadow)

    assert interpreter.is_symlink()
    assert _find_fpms_package(launcher) == package.resolve()
    install_fpms(_find_fpms_package(launcher), ADAPTER)
    assert (stale / "fpms.py").read_text() == "# stale package\n"
    assert (package / "modules" / "apps" / "channel_utilization.py").read_bytes() == ADAPTER.read_bytes()


def test_reinstall_after_package_update_reapplies_current_integration(
    installation: tuple[Path, Path, Path],
) -> None:
    launcher, _, package = installation
    install_fpms(_find_fpms_package(launcher), ADAPTER)
    # Simulate dpkg replacing the upstream files and removing our adapter.
    (package / "fpms.py").write_bytes(
        (FIXTURES / "fpms.py").read_bytes() + b"\n# updated upstream package\n"
    )
    shutil.copy2(FIXTURES / "buttons.py", package / "modules" / "nav" / "buttons.py")
    (package / "modules" / "apps" / "channel_utilization.py").unlink()
    install_fpms(_find_fpms_package(launcher), ADAPTER)
    text = (package / "fpms.py").read_text()
    assert "# updated upstream package" in text
    assert text.count("build_channel_utilization_menu(g_vars)") == 1
    assert "page_exit_handler" in (package / "modules" / "nav" / "buttons.py").read_text()
    assert (package / "modules" / "apps" / "channel_utilization.py").read_bytes() == ADAPTER.read_bytes()


def test_unsupported_active_layout_does_not_fall_back_to_stale_package(
    installation: tuple[Path, Path, Path],
) -> None:
    launcher, interpreter, package = installation
    stale = interpreter.parent.parent / "lib" / "python3.11" / "site-packages" / "fpms"
    stale.mkdir(parents=True)
    (stale / "fpms.py").write_text("# stale\n")
    buttons = package / "modules" / "nav" / "buttons.py"
    buttons.unlink()
    before = (package / "fpms.py").read_bytes()
    with pytest.raises(FPMSInstallError, match="Unsupported FPMS package layout"):
        install_fpms(_find_fpms_package(launcher), ADAPTER)
    assert (package / "fpms.py").read_bytes() == before
    assert not (package / "fpms.py.beacon-live.bak").exists()
    assert (stale / "fpms.py").read_text() == "# stale\n"


def test_missing_launcher_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FPMSInstallError, match="Cannot read FPMS launcher"):
        _find_fpms_package(tmp_path / "missing")


@pytest.mark.parametrize("shebang", [
    "#!/usr/bin/env python3", "#!/bin/sh", "#!relative/python",
    "#!/usr/bin/python3 -s", "# not a shebang",
])
def test_unsupported_launcher_is_reported(tmp_path: Path, shebang: str) -> None:
    launcher = tmp_path / "fpms"
    launcher.write_text(shebang + "\n")
    with pytest.raises(FPMSInstallError, match="Unsupported FPMS launcher"):
        _find_fpms_package(launcher)


def test_missing_interpreter_is_reported(tmp_path: Path) -> None:
    launcher = tmp_path / "fpms"
    launcher.write_text(f"#!{tmp_path}/python\n")
    with pytest.raises(FPMSInstallError, match="interpreter is missing or not executable"):
        _find_fpms_package(launcher)


@pytest.mark.parametrize("response", ["not json", "[]", "{}", '["relative"]', '[1]', '["/a", "/b"]'])
def test_invalid_package_location_is_reported(tmp_path: Path, response: str) -> None:
    launcher = tmp_path / "fpms"
    launcher.write_text(f"#!{sys.executable}\n")
    def run(command, **kwargs):
        assert command[:3] == [sys.executable, "-I", "-c"]
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(command, 0, stdout=response)
    with pytest.raises(FPMSInstallError):
        _find_fpms_package(launcher, command_runner=run)


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_failed_interpreter_probe_is_reported(tmp_path: Path, failure: str) -> None:
    launcher = tmp_path / "fpms"
    launcher.write_text(f"#!{sys.executable}\n")
    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="broken environment")
    with pytest.raises(FPMSInstallError, match="Could not query|broken environment"):
        _find_fpms_package(launcher, command_runner=run)
