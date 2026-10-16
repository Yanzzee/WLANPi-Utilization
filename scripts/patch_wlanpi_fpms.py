#!/usr/bin/env python3
"""Install the Utilization adapter into WLAN Pi FPMS on Trixie."""

from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class FPMSInstallError(RuntimeError):
    """The FPMS integration could not be installed safely."""


@dataclass(frozen=True)
class FileState:
    content: bytes | None
    mode: int = 0o644

    @classmethod
    def read(cls, path: Path) -> FileState:
        if not path.exists():
            return cls(None)
        return cls(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fpms-package", type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--restart-service", action="store_true")
    parser.add_argument("--capture-backend", choices=("direct", "core"))
    parser.add_argument("--runtime-mode", choices=("standalone", "managed"))
    parser.add_argument("--backend-config", type=Path, default=Path("/etc/wlanpi-beacon-live/fpms.json"))
    args = parser.parse_args()

    try:
        package = args.fpms_package or _find_fpms_package()
        print(f"Resolved FPMS Python package: {package}", flush=True)
        install_fpms(package, args.adapter, restart_service=args.restart_service,
                     capture_backend=args.capture_backend, backend_config=args.backend_config,
                     runtime_mode=args.runtime_mode)
    except (FPMSInstallError, OSError, SyntaxError, UnicodeError) as error:
        raise SystemExit(f"Utilization FPMS installation failed: {error}") from error
    print(f"Installed Utilization FPMS adapter in {package}")
    return 0


def install_fpms(
    package: Path,
    adapter: Path,
    *,
    restart_service: bool = False,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    capture_backend: str | None = None,
    runtime_mode: str | None = None,
    backend_config: Path = Path("/etc/wlanpi-beacon-live/fpms.json"),
) -> None:
    """Validate first, then install and recover the pre-install files on failure."""
    fpms_file = package / "fpms.py"
    buttons_file = package / "modules" / "nav" / "buttons.py"
    apps_dir = package / "modules" / "apps"
    if (
        not fpms_file.is_file()
        or not buttons_file.is_file()
        or not apps_dir.is_dir()
    ):
        raise FPMSInstallError(f"Unsupported FPMS package layout: {package}")

    adapter_file = apps_dir / "channel_utilization.py"
    originals = {
        path: FileState.read(path)
        for path in (fpms_file, buttons_file, adapter_file)
    }
    replacements = {
        fpms_file: _patch_fpms(
            fpms_file.read_text(encoding="utf-8"), fpms_file
        ).encode("utf-8"),
        buttons_file: _patch_buttons(
            buttons_file.read_text(encoding="utf-8"), buttons_file
        ).encode("utf-8"),
        adapter_file: adapter.read_bytes(),
    }
    # Compile every proposed file before backups, writes, or service operations.
    for path, content in replacements.items():
        compile(content, str(path), "exec")

    if capture_backend is not None or runtime_mode is not None:
        if capture_backend is not None and capture_backend not in {"direct", "core"}:
            raise FPMSInstallError("Capture backend must be direct or core")
        if runtime_mode is not None and runtime_mode not in {"standalone", "managed"}:
            raise FPMSInstallError("Runtime mode must be standalone or managed")
        original = FileState.read(backend_config)
        try:
            settings = json.loads(original.content) if original.content is not None else {}
            if not isinstance(settings, dict):
                raise ValueError("expected a JSON object")
        except ValueError as error:
            raise FPMSInstallError(f"Invalid backend configuration {backend_config}: {error}") from error
        if capture_backend is not None:
            settings["capture_backend"] = capture_backend
        if runtime_mode is not None:
            settings["runtime_mode"] = runtime_mode
        originals[backend_config] = original
        replacements[backend_config] = (json.dumps(settings, indent=2) + "\n").encode()

    writes: dict[Path, FileState] = {}
    for path, content in replacements.items():
        original = originals[path]
        if content == original.content:
            continue
        backup = path.with_suffix(path.suffix + ".beacon-live.bak")
        if original.content is not None and not backup.exists():
            originals[backup] = FileState(None)
            writes[backup] = original
        writes[path] = FileState(content, original.mode)

    attempted: list[Path] = []
    service_attempted = False
    try:
        for path, replacement in writes.items():
            if path == backend_config:
                path.parent.mkdir(parents=True, exist_ok=True)
            attempted.append(path)
            _atomic_write(path, replacement)
        if restart_service:
            service_attempted = True
            _restart_and_verify(command_runner, sleep)
    except BaseException as error:
        recovery_errors: list[str] = []
        for path in reversed(attempted):
            try:
                _atomic_write(path, originals[path])
            except OSError as recovery_error:
                recovery_errors.append(f"{path}: {recovery_error}")
        if service_attempted and not recovery_errors:
            try:
                _restart_and_verify(command_runner, sleep)
            except (OSError, subprocess.CalledProcessError) as recovery_error:
                recovery_errors.append(f"restored FPMS service: {recovery_error}")
        if recovery_errors:
            raise FPMSInstallError(
                f"{error}; recovery failed: {'; '.join(recovery_errors)}. "
                "Inspect systemctl status and journalctl -u wlanpi-fpms."
            ) from error
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise FPMSInstallError(
            f"{error}; restored the pre-install FPMS files."
        ) from error


def _atomic_write(path: Path, state: FileState) -> None:
    if state.content is None:
        path.unlink(missing_ok=True)
        return
    # Replace within the same directory so FPMS never reads a truncated file.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(state.content)
            stream.flush()
            os.chmod(temporary, state.mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _restart_and_verify(command_runner: Callable, sleep: Callable) -> None:
    command_runner(["systemctl", "restart", "wlanpi-fpms"], check=True)
    # Type=simple can report success before Python imports finish. Check that
    # the service remains active rather than trusting the restart exit code.
    for _ in range(5):
        sleep(1)
        command_runner(
            ["systemctl", "is-active", "--quiet", "wlanpi-fpms"], check=True
        )


def _find_fpms_package(
    launcher: Path = Path("/usr/sbin/fpms"),
    *,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """Locate the package visible to the launcher's Python without importing it."""
    try:
        target = launcher.resolve(strict=True)
        with target.open(encoding="utf-8") as stream:
            shebang = stream.readline(4096).strip()
        arguments = shlex.split(shebang[2:]) if shebang.startswith("#!") else []
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        raise FPMSInstallError(f"Cannot read FPMS launcher {launcher}: {error}") from error

    if (
        len(arguments) != 1
        or not Path(arguments[0]).is_absolute()
        or not Path(arguments[0]).name.startswith("python")
    ):
        raise FPMSInstallError(
            f"Unsupported FPMS launcher {launcher} ({target}): expected a direct "
            "absolute Python shebang. Use --fpms-package for an explicit layout."
        )
    interpreter = Path(arguments[0])
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise FPMSInstallError(f"FPMS interpreter is missing or not executable: {interpreter}")

    # Keep the venv interpreter path from the shebang. Resolving its symlink to
    # /usr/bin/python would select the system site-packages instead of FPMS's.
    # Isolated mode also excludes the checkout, PYTHONPATH, and user packages.
    probe = (
        "import importlib.util, json; "
        "spec = importlib.util.find_spec('fpms'); "
        "print(json.dumps(list(spec.submodule_search_locations or []) "
        "if spec is not None else []))"
    )
    try:
        result = command_runner(
            [str(interpreter), "-I", "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FPMSInstallError(
            f"Could not query FPMS interpreter {interpreter}: {error}"
        ) from error
    if result.returncode != 0:
        raise FPMSInstallError(
            f"FPMS interpreter {interpreter} exited with status {result.returncode}: "
            f"{(result.stderr or '').strip()}"
        )
    try:
        locations = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise FPMSInstallError(
            f"Invalid package location from FPMS interpreter {interpreter}"
        ) from error
    if (
        not isinstance(locations, list)
        or len(locations) != 1
        or not isinstance(locations[0], str)
        or not Path(locations[0]).is_absolute()
        or not Path(locations[0]).is_dir()
    ):
        raise FPMSInstallError(
            f"Could not uniquely locate an installed fpms package through {interpreter}"
        )
    return Path(locations[0]).resolve()


def _patch_fpms(text: str, path: Path) -> str:
    import_line = (
        "from .modules.apps.channel_utilization import "
        "build_channel_utilization_menu\n"
    )
    if import_line not in text:
        anchor = "from .modules.apps.scanner import *\n"
        if anchor not in text:
            raise FPMSInstallError(f"FPMS import anchor not found in {path}")
        text = text.replace(anchor, anchor + import_line, 1)

    menu_line = "            build_channel_utilization_menu(g_vars),\n"
    text = text.replace(menu_line, "")

    append_code = (
        "    # Keep the third-party Utilization app last in the Apps menu.\n"
        "    for menu_item in menu:\n"
        "        if menu_item[\"name\"] == \"Apps\":\n"
        "            menu_item[\"action\"].append(\n"
        "                build_channel_utilization_menu(g_vars)\n"
        "            )\n"
        "            break\n\n"
    )
    if append_code not in text:
        anchor = "    # update menu options data structure if we're in non-classic mode\n"
        if anchor not in text:
            raise FPMSInstallError(f"FPMS post-menu anchor not found in {path}")
        text = text.replace(anchor, append_code + anchor, 1)

    callbacks = {
        "channel_utilization_return_to_menu": "menu_left",
        "channel_utilization_toggle_orientation": "toggle_display_orientation",
    }
    for callback_name, function_name in callbacks.items():
        _find_function(text, function_name, path)
        binding = f"    g_vars['{callback_name}'] = {function_name}\n"
        if binding not in text:
            # Upgrade existing patches without registering a second app.
            text = text.replace(append_code, binding + append_code, 1)

    # The three auxiliary-key dispatchers live in fpms.py, not Button.
    for button_number in (1, 2, 3):
        text = _insert_page_handler(
            text,
            method_name=f"menu_key{button_number}",
            handler_name=f"page_key{button_number}_handler",
            path=path,
        )

    return text


def _patch_buttons(text: str, path: Path) -> str:
    handler_code = (
        "            exit_handler = g_vars.pop('page_exit_handler', None)\n"
        "            if callable(exit_handler):\n"
        "                exit_handler()\n"
    )
    if handler_code not in text:
        method = _find_function(text, "menu_left", path)
        # Match Python syntax, not quote style, whitespace, or nearby methods.
        condition = ast.parse("g_vars['display_state'] == 'page'", mode="eval").body
        branches = [
            node for node in method.body
            if isinstance(node, ast.If)
            and ast.dump(node.test) == ast.dump(condition)
        ]
        if len(branches) != 1:
            raise FPMSInstallError(f"FPMS page-exit branch not found in {path}")
        insertion = _line_offset(text, branches[0].body[0].lineno)
        text = text[:insertion] + handler_code + text[insertion:]

    text = _insert_page_handler(
        text,
        method_name="menu_up",
        handler_name="page_up_handler",
        path=path,
    )
    text = _insert_page_handler(
        text,
        method_name="menu_down",
        handler_name="page_down_handler",
        path=path,
    )
    return text


def _insert_page_handler(
    text: str,
    *,
    method_name: str,
    handler_name: str,
    path: Path,
) -> str:
    handler_code = (
        "        if g_vars.get('display_state') == 'page':\n"
        f"            page_handler = g_vars.get('{handler_name}')\n"
        "            if callable(page_handler):\n"
        "                page_handler()\n"
        "                return\n"
    )
    if handler_code in text:
        return text

    method = _find_function(text, method_name, path)
    first_statement = method.body[0]
    if (
        isinstance(first_statement, ast.Expr)
        and isinstance(first_statement.value, ast.Constant)
        and isinstance(first_statement.value.value, str)
    ):
        insertion = _line_offset(text, first_statement.end_lineno + 1)
    else:
        insertion = _line_offset(text, first_statement.lineno)
    return text[:insertion] + handler_code + text[insertion:]


def _find_function(text: str, name: str, path: Path) -> ast.FunctionDef:
    functions = [
        node for node in ast.walk(ast.parse(text, filename=str(path)))
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(functions) != 1:
        raise FPMSInstallError(f"Could not uniquely locate FPMS {name} in {path}")
    return functions[0]


def _line_offset(text: str, line_number: int) -> int:
    return sum(
        len(line) for line in text.splitlines(keepends=True)[:line_number - 1]
    )


if __name__ == "__main__":
    raise SystemExit(main())
