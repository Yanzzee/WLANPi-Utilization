#!/usr/bin/env python3
"""Install the Utilization adapter into WLAN Pi FPMS 2.x."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fpms-package", type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    args = parser.parse_args()

    package = args.fpms_package or _find_fpms_package()
    fpms_file = package / "fpms.py"
    buttons_file = package / "modules" / "nav" / "buttons.py"
    apps_dir = package / "modules" / "apps"
    if not fpms_file.is_file() or not buttons_file.is_file():
        raise SystemExit(f"Unsupported FPMS package layout: {package}")

    apps_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.adapter, apps_dir / "channel_utilization.py")
    _patch_fpms(fpms_file)
    _patch_buttons(buttons_file)
    print(f"Installed Utilization FPMS adapter in {package}")
    return 0


def _find_fpms_package() -> Path:
    candidates = sorted(
        Path("/opt/wlanpi-fpms/lib").glob("python*/site-packages/fpms")
    )
    if len(candidates) != 1:
        found = ", ".join(str(path) for path in candidates) or "none"
        raise SystemExit(
            "Could not uniquely locate the FPMS Python package under "
            f"/opt/wlanpi-fpms (found: {found})"
        )
    return candidates[0]


def _patch_fpms(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    import_line = (
        "from .modules.apps.channel_utilization import "
        "build_channel_utilization_menu\n"
    )
    if import_line not in text:
        anchor = "from .modules.apps.scanner import *\n"
        if anchor not in text:
            raise SystemExit(f"FPMS import anchor not found in {path}")
        text = text.replace(anchor, anchor + import_line, 1)

    menu_line = "            build_channel_utilization_menu(g_vars),\n"
    if menu_line not in text:
        anchor = '        {"name": "Apps", "action": [\n'
        if anchor not in text:
            raise SystemExit(f"FPMS Apps menu anchor not found in {path}")
        text = text.replace(anchor, anchor + menu_line, 1)

    _write_with_backup(path, text)


def _patch_buttons(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    handler_code = (
        "            exit_handler = g_vars.pop('page_exit_handler', None)\n"
        "            if callable(exit_handler):\n"
        "                exit_handler()\n"
    )
    if handler_code in text:
        return

    start = text.find("    def menu_left(self, g_vars, menu):")
    if start < 0:
        raise SystemExit(f"FPMS menu_left method not found in {path}")
    page_branch = text.find("        if g_vars['display_state'] == 'page':\n", start)
    if page_branch < 0:
        raise SystemExit(f"FPMS page-exit branch not found in {path}")
    insertion = page_branch + len(
        "        if g_vars['display_state'] == 'page':\n"
    )
    text = text[:insertion] + handler_code + text[insertion:]
    _write_with_backup(path, text)


def _write_with_backup(path: Path, text: str) -> None:
    backup = path.with_suffix(path.suffix + ".beacon-live.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
