import os
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.install_cli_launchers import COMMANDS
from scripts.install_cli_launchers import MANAGED_MARKER
from scripts.install_cli_launchers import install_launchers


def _create_targets(app_bin: Path) -> None:
    app_bin.mkdir(parents=True)
    for command in COMMANDS:
        target = app_bin / command
        target.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$0" "$@"\n',
            encoding="utf-8",
        )
        target.chmod(0o755)


def test_install_launchers_creates_executable_wrappers_for_app_targets(
    tmp_path: Path,
) -> None:
    app_bin = tmp_path / "opt" / "wlanpi-beacon-live" / "bin"
    launcher_dir = tmp_path / "usr" / "local" / "bin"
    _create_targets(app_bin)

    launchers = install_launchers(app_bin=app_bin, launcher_dir=launcher_dir)

    assert launchers == tuple(launcher_dir / command for command in COMMANDS)
    for command, launcher in zip(COMMANDS, launchers):
        text = launcher.read_text(encoding="utf-8")
        assert MANAGED_MARKER in text
        assert str(app_bin / command) in text
        assert 'exec "$target" "$@"' in text
        assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
        assert os.access(launcher, os.X_OK)
        completed = subprocess.run(
            [str(launcher), "first", "two words"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert completed.stdout.splitlines() == [
            str(app_bin / command),
            "first",
            "two words",
        ]


def test_install_launchers_is_idempotent_and_refreshes_managed_files(
    tmp_path: Path,
) -> None:
    app_bin = tmp_path / "app" / "bin"
    launcher_dir = tmp_path / "commands"
    _create_targets(app_bin)
    install_launchers(app_bin=app_bin, launcher_dir=launcher_dir)

    stale_launcher = launcher_dir / COMMANDS[0]
    stale_launcher.write_text(
        f"#!/bin/sh\n{MANAGED_MARKER}\nexit 1\n",
        encoding="utf-8",
    )
    stale_launcher.chmod(0o600)

    install_launchers(app_bin=app_bin, launcher_dir=launcher_dir)
    refreshed = {
        path.name: (path.read_text(encoding="utf-8"), path.stat().st_mode)
        for path in launcher_dir.iterdir()
    }
    install_launchers(app_bin=app_bin, launcher_dir=launcher_dir)

    assert {path.name for path in launcher_dir.iterdir()} == set(COMMANDS)
    for command in COMMANDS:
        launcher = launcher_dir / command
        assert (
            launcher.read_text(encoding="utf-8"),
            launcher.stat().st_mode,
        ) == refreshed[command]
        assert str(app_bin / command) in refreshed[command][0]
        assert stat.S_IMODE(refreshed[command][1]) == 0o755


@pytest.mark.parametrize("missing_command", COMMANDS)
def test_install_launchers_fails_before_creation_when_target_is_invalid(
    tmp_path: Path,
    missing_command: str,
) -> None:
    app_bin = tmp_path / "app" / "bin"
    launcher_dir = tmp_path / "commands"
    _create_targets(app_bin)
    invalid_target = app_bin / missing_command
    if missing_command == "wlanpi-beacon-live":
        invalid_target.unlink()
    else:
        invalid_target.chmod(0o644)

    with pytest.raises(SystemExit, match="target is missing or not executable"):
        install_launchers(app_bin=app_bin, launcher_dir=launcher_dir)

    assert not launcher_dir.exists()


def test_install_launchers_does_not_replace_an_unmanaged_command(
    tmp_path: Path,
) -> None:
    app_bin = tmp_path / "app" / "bin"
    launcher_dir = tmp_path / "commands"
    _create_targets(app_bin)
    launcher_dir.mkdir()
    unmanaged = launcher_dir / "beacon-live"
    unmanaged.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="unmanaged CLI launcher"):
        install_launchers(app_bin=app_bin, launcher_dir=launcher_dir)

    assert unmanaged.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
    assert not (launcher_dir / "wlanpi-beacon-live").exists()
