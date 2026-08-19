#!/usr/bin/env python3
"""Install system-wide launchers for the isolated Beacon Live environment."""

from __future__ import annotations

import argparse
import os
import shlex
import tempfile
from pathlib import Path
from typing import Optional


COMMANDS = ("wlanpi-beacon-live", "beacon-live")
MANAGED_MARKER = "# Managed by the WLANPi Beacon Live installer."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--app-bin",
        type=Path,
        default=Path("/opt/wlanpi-beacon-live/bin"),
    )
    parser.add_argument(
        "--launcher-dir",
        type=Path,
        default=Path("/usr/local/bin"),
    )
    args = parser.parse_args()

    install_launchers(app_bin=args.app_bin, launcher_dir=args.launcher_dir)
    return 0


def install_launchers(*, app_bin: Path, launcher_dir: Path) -> tuple[Path, ...]:
    """Atomically create or refresh the two managed command launchers."""
    app_bin = app_bin.absolute()
    launcher_dir = launcher_dir.absolute()
    targets = {command: app_bin / command for command in COMMANDS}

    invalid_targets = [
        target
        for target in targets.values()
        if not target.is_file() or not os.access(target, os.X_OK)
    ]
    if invalid_targets:
        missing = ", ".join(str(path) for path in invalid_targets)
        raise SystemExit(
            "Cannot install Beacon Live CLI launchers; target is missing or "
            f"not executable: {missing}"
        )

    try:
        launcher_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"Cannot create CLI launcher directory {launcher_dir}: {exc}"
        ) from exc
    if not launcher_dir.is_dir():
        raise SystemExit(f"CLI launcher path is not a directory: {launcher_dir}")

    launchers = tuple(launcher_dir / command for command in COMMANDS)
    for launcher, target in zip(launchers, targets.values()):
        _require_managed_or_absent(launcher, target)

    for launcher, target in zip(launchers, targets.values()):
        _write_launcher(launcher, target)
        print(f"Installed CLI launcher {launcher} -> {target}")
    return launchers


def _require_managed_or_absent(path: Path, target: Path) -> None:
    if path.is_symlink():
        linked_path = Path(os.readlink(path))
        if not linked_path.is_absolute():
            linked_path = path.parent / linked_path
        if linked_path.absolute() == target:
            return
        raise SystemExit(
            f"Refusing to replace unmanaged CLI launcher {path}; "
            f"it points to {linked_path}"
        )
    if not path.exists():
        return
    if not path.is_file():
        raise SystemExit(f"Refusing to replace non-file CLI launcher path {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit(
            f"Refusing to replace unreadable CLI launcher {path}: {exc}"
        ) from exc
    if MANAGED_MARKER not in text.splitlines()[:3]:
        raise SystemExit(f"Refusing to replace unmanaged CLI launcher {path}")


def _write_launcher(path: Path, target: Path) -> None:
    target_text = shlex.quote(str(target))
    content = (
        "#!/bin/sh\n"
        f"{MANAGED_MARKER}\n"
        f"target={target_text}\n"
        'if [ ! -x "$target" ]; then\n'
        f'    echo "{path.name}: target is missing or not executable: '
        '$target" >&2\n'
        "    exit 127\n"
        "fi\n"
        'exec "$target" "$@"\n'
    )

    temporary_path: Optional[Path] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            os.fchmod(temporary_file.fileno(), 0o755)
        os.replace(temporary_path, path)
        temporary_path = None
        path.chmod(0o755)
    except OSError as exc:
        raise SystemExit(f"Cannot install CLI launcher {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
