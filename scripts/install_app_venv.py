#!/usr/bin/env python3
"""Copy the app into its venv and install optional isolated runtime wheels."""

from __future__ import annotations

import argparse
import shutil
import site
import stat
import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--with-core", action="store_true")
    parser.add_argument("--with-web", action="store_true")
    parser.add_argument("--wheel-dir", type=Path, help="Install optional dependencies offline from this wheel directory")
    args = parser.parse_args()

    source_package = args.source_root / "beacon_live"
    if not (source_package / "device.py").is_file():
        raise SystemExit(f"beacon_live source package not found in {args.source_root}")

    site_packages = _venv_site_packages()
    if args.wheel_dir is not None and not (args.with_core or args.with_web):
        parser.error("--wheel-dir requires --with-core or --with-web")
    if args.with_core:
        _install_runtime_extra(args.source_root, wheel_directory=args.wheel_dir, extra="core")
    if args.with_web:
        _install_runtime_extra(args.source_root, wheel_directory=args.wheel_dir, extra="web")
    target_package = site_packages / "beacon_live"
    if target_package.exists():
        shutil.rmtree(target_package)
    shutil.copytree(
        source_package,
        target_package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    bin_dir = Path(sys.executable).parent
    _write_entrypoint(bin_dir / "wlanpi-utilization", "beacon_live.device")
    _write_entrypoint(bin_dir / "utilization", "beacon_live.cli")
    _write_entrypoint(bin_dir / "wlanpi-beacon-live", "beacon_live.device")
    _write_entrypoint(bin_dir / "beacon-live", "beacon_live.cli")
    print(f"Installed beacon_live in {site_packages}")
    if args.with_core:
        subprocess.run([sys.executable, "-I", "-c",
            "from beacon_live.core_capture import check_core_dependency; check_core_dependency()"], check=True)
    return 0


def _install_runtime_extra(source_root: Path, *, extra: str,
                           wheel_directory: Path | None = None, command_runner=subprocess.run) -> None:
    """Install only declared runtime wheels, never build or borrow Core packages."""
    _venv_site_packages()  # Must precede any pip invocation.
    configuration = tomllib.loads((source_root / "pyproject.toml").read_text())
    requirements = configuration["project"]["optional-dependencies"][extra]
    if (not isinstance(requirements, list) or not requirements
            or any(not isinstance(item, str) or item.startswith('-') for item in requirements)):
        raise SystemExit(f"Invalid {extra} dependency declarations")
    command = [sys.executable, "-I", "-m", "pip", "--isolated", "--no-cache-dir", "--disable-pip-version-check",
               "install", "--only-binary=:all:"]
    if wheel_directory is not None:
        if not wheel_directory.is_dir():
            raise SystemExit(f"{extra} wheel directory not found: {wheel_directory}")
        command.extend(["--no-index", "--find-links", str(wheel_directory.resolve())])
    command.extend(requirements)
    command_runner(command, check=True)
    print(f"Installed {extra} dependencies in the application venv")


def _venv_site_packages() -> Path:
    if sys.version_info < (3, 13):
        raise SystemExit(f"Python 3.13+ is required; found {sys.version.split()[0]}")
    if sys.prefix == sys.base_prefix:
        raise SystemExit("Installation requires a virtual environment; system Python will not be modified.")
    venv_root = Path(sys.prefix).resolve()
    try:
        config = (venv_root / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Cannot verify virtual environment {venv_root}: {error}") from error
    settings = {
        key.strip().lower(): value.strip().lower()
        for key, value in (line.split("=", 1) for line in config.splitlines() if "=" in line)
    }
    if settings.get("include-system-site-packages") == "true":
        raise SystemExit("Installation requires a venv without system site-packages.")
    candidates = [Path(path) for path in site.getsitepackages()]
    for candidate in candidates:
        if candidate.resolve().is_relative_to(venv_root):
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    raise SystemExit(f"Could not find site-packages under {sys.prefix}")


def _write_entrypoint(path: Path, module: str) -> None:
    content = (
        f"#!{sys.executable}\n"
        f"from {module} import main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


if __name__ == "__main__":
    raise SystemExit(main())
