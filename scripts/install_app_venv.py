#!/usr/bin/env python3
"""Install beacon_live into the current interpreter's venv without pip."""

from __future__ import annotations

import argparse
import shutil
import site
import stat
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()

    source_package = args.source_root / "beacon_live"
    if not (source_package / "device.py").is_file():
        raise SystemExit(f"beacon_live source package not found in {args.source_root}")

    site_packages = _venv_site_packages()
    target_package = site_packages / "beacon_live"
    if target_package.exists():
        shutil.rmtree(target_package)
    shutil.copytree(
        source_package,
        target_package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    bin_dir = Path(sys.executable).parent
    _write_entrypoint(bin_dir / "wlanpi-beacon-live", "beacon_live.device")
    _write_entrypoint(bin_dir / "beacon-live", "beacon_live.cli")
    print(f"Installed beacon_live in {site_packages}")
    return 0


def _venv_site_packages() -> Path:
    candidates = [Path(path) for path in site.getsitepackages()]
    for candidate in candidates:
        if str(candidate).startswith(sys.prefix):
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    raise SystemExit(f"Could not find site-packages under {sys.prefix}")


def _write_entrypoint(path: Path, module: str) -> None:
    content = (
        f"#!{sys.executable}\n"
        f"from {module} import main\n"
        "raise SystemExit(main())\n"
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


if __name__ == "__main__":
    raise SystemExit(main())
