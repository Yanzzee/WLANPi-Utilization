import ast
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from scripts import patch_wlanpi_fpms as patcher


FIXTURES = Path(__file__).parent / "fixtures" / "fpms_1_4_24"
ADAPTER = Path(__file__).parents[1] / "integration" / "wlanpi_fpms" / "channel_utilization.py"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    package = tmp_path / "fpms"
    (package / "modules" / "nav").mkdir(parents=True)
    (package / "modules" / "apps").mkdir()
    shutil.copy2(FIXTURES / "fpms.py", package / "fpms.py")
    shutil.copy2(FIXTURES / "buttons.py", package / "modules" / "nav" / "buttons.py")
    return package


def snapshot(package: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(package)): (
            path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
        )
        for path in package.rglob("*") if path.is_file()
    }


def test_trixie_install_adds_one_app_and_is_idempotent(package: Path) -> None:
    original = snapshot(package)
    patcher.install_fpms(package, ADAPTER)
    installed = snapshot(package)
    patcher.install_fpms(package, ADAPTER)
    assert snapshot(package) == installed
    assert installed["modules/apps/channel_utilization.py"][0] == ADAPTER.read_bytes()
    for source in ("fpms.py", "modules/nav/buttons.py"):
        assert installed[source + ".beacon-live.bak"] == original[source]

    # Execute the actual upstream menu expression and appended registration,
    # without importing its hardware modules or starting its event loop.
    tree = ast.parse((package / "fpms.py").read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    menu_index = next(
        index for index, node in enumerate(main.body)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name) and node.target.id == "menu"
    )
    registration = main.body[menu_index + 1]
    assert isinstance(registration, ast.For)
    menu_assignment = main.body[menu_index]
    nodes = [ast.Assign(targets=[menu_assignment.target], value=menu_assignment.value), registration]
    namespace = {
        node.id: (lambda: None)
        for statement in nodes for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    namespace.update({
        "g_vars": {"timezones_available": []},
        "build_channel_utilization_menu": lambda g_vars: {"name": "Utilization", "action": []},
    })
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, "trixie_menu", "exec"), namespace)
    apps = next(item["action"] for item in namespace["menu"] if item["name"] == "Apps")
    assert [item["name"] for item in apps] == ["Kismet", "Profiler", "Scanner", "Utilization"]


def test_trixie_buttons_dispatch_and_exit_return_to_menu(package: Path) -> None:
    patcher.install_fpms(package, ADAPTER)
    tree = ast.parse((package / "modules" / "nav" / "buttons.py").read_text())
    button_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    namespace = {}
    exec(compile(ast.Module(body=[button_class], type_ignores=[]), "trixie_buttons", "exec"), namespace)
    button = object.__new__(namespace["Button"])
    calls = []
    class Page:
        def draw_page(self, g_vars, menu):
            calls.append("menu")
    button.page_obj = Page()
    state = {
        "display_state": "page",
        "current_menu_location": [2, 3, 0],
        "page_up_handler": lambda: calls.append("up"),
        "page_down_handler": lambda: calls.append("down"),
        "page_exit_handler": lambda: calls.append("exit"),
    }
    button.menu_up(state, [])
    button.menu_down(state, [])
    button.menu_left(state, [])
    assert calls == ["up", "down", "exit", "menu"]
    assert state["display_state"] == "menu"
    assert "page_exit_handler" not in state


@pytest.mark.parametrize(
    "failure", ["branch", "dispatcher", "import", "menu", "adapter_syntax"]
)
def test_validation_failure_leaves_every_file_unchanged(package: Path, tmp_path: Path, failure: str) -> None:
    adapter = ADAPTER
    if failure == "adapter_syntax":
        adapter = tmp_path / "invalid_adapter.py"
        adapter.write_text("def broken(\n")
    else:
        path = (
            package / "fpms.py" if failure in {"import", "menu"}
            else package / "modules" / "nav" / "buttons.py"
        )
        text = path.read_text()
        if failure == "branch":
            text = text.replace('== "page":', '== "unknown":')
        elif failure == "dispatcher":
            text = text.replace("def menu_down(", "def renamed_menu_down(")
        elif failure == "import":
            text = text.replace("from .modules.apps.scanner import *", "# scanner removed")
        else:
            text = text.replace("# update menu options data structure if we're in non-classic mode", "# menu restructured")
        path.write_text(text)
    before = snapshot(package)
    with pytest.raises((patcher.FPMSInstallError, SyntaxError)):
        patcher.install_fpms(package, adapter)
    assert snapshot(package) == before


@pytest.mark.parametrize("existing_adapter", [False, True])
def test_write_failure_rolls_back_files_modes_and_backups(package: Path, monkeypatch: pytest.MonkeyPatch, existing_adapter: bool) -> None:
    adapter_path = package / "modules" / "apps" / "channel_utilization.py"
    if existing_adapter:
        adapter_path.write_text("# previous adapter\n")
        adapter_path.chmod(0o640)
    backup = package / "fpms.py.beacon-live.bak"
    backup.write_bytes(b"existing backup must survive")
    (package / "fpms.py").chmod(0o640)
    before = snapshot(package)
    write = patcher._atomic_write
    def fail_after_adapter_write(path, state):
        write(path, state)
        if path == adapter_path and state.content == ADAPTER.read_bytes():
            raise OSError("simulated write failure")
    monkeypatch.setattr(patcher, "_atomic_write", fail_after_adapter_write)
    with pytest.raises(patcher.FPMSInstallError, match="restored the pre-install"):
        patcher.install_fpms(package, ADAPTER)
    assert snapshot(package) == before


def test_service_stays_active_before_install_reports_success(package: Path) -> None:
    calls = []
    waits = []
    def run(command, *, check):
        assert check
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    patcher.install_fpms(package, ADAPTER, restart_service=True, command_runner=run, sleep=waits.append)
    assert calls == [["systemctl", "restart", "wlanpi-fpms"]] + [
        ["systemctl", "is-active", "--quiet", "wlanpi-fpms"]
    ] * 5
    assert waits == [1] * 5


@pytest.mark.parametrize("failed_command", ["restart", "is-active"])
def test_service_failure_restores_files_and_restarts_previous_service(package: Path, failed_command: str) -> None:
    before = snapshot(package)
    restart_count = 0
    def run(command, *, check):
        nonlocal restart_count
        if command[1] == "restart":
            restart_count += 1
        if restart_count == 1 and command[1] == failed_command:
            raise subprocess.CalledProcessError(1, command)
        if restart_count == 2:
            assert snapshot(package) == before
        return subprocess.CompletedProcess(command, 0)
    with pytest.raises(patcher.FPMSInstallError, match="restored the pre-install"):
        patcher.install_fpms(package, ADAPTER, restart_service=True, command_runner=run, sleep=lambda seconds: None)
    assert restart_count == 2
    assert snapshot(package) == before


def test_reports_service_recovery_failure(package: Path) -> None:
    before = snapshot(package)
    def run(command, *, check):
        raise subprocess.CalledProcessError(1, command)
    with pytest.raises(patcher.FPMSInstallError, match="recovery failed.*restored FPMS service"):
        patcher.install_fpms(package, ADAPTER, restart_service=True, command_runner=run, sleep=lambda seconds: None)
    assert snapshot(package) == before


def test_previous_partial_install_is_repaired(package: Path) -> None:
    fpms = package / "fpms.py"
    fpms.write_text(patcher._patch_fpms(fpms.read_text(), fpms))
    patcher.install_fpms(package, ADAPTER)
    installed = snapshot(package)
    patcher.install_fpms(package, ADAPTER)
    assert snapshot(package) == installed
    assert b"page_exit_handler" in installed["modules/nav/buttons.py"][0]


def test_trixie_auxiliary_handlers_preserve_native_key2_outside_app(package: Path) -> None:
    patcher.install_fpms(package, ADAPTER)
    tree = ast.parse((package / "fpms.py").read_text())
    dispatchers = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"menu_key1", "menu_key2", "menu_key3"}
    ]
    calls = []
    class Page:
        def __init__(self, g_vars):
            pass

        def draw_page(self, g_vars, menu):
            calls.append("menu")

    state = {
        "display_state": "page",
        **{f"page_key{key}_handler": lambda key=key: calls.append(key) for key in (1, 2, 3)},
    }
    namespace = {
        "g_vars": state, "menu": [], "Page": Page,
        "toggle_display_orientation": lambda: calls.append("rotate"),
    }
    exec(compile(ast.Module(body=dispatchers, type_ignores=[]), "trixie_keys", "exec"), namespace)
    for key in (1, 2, 3):
        namespace[f"menu_key{key}"]()
    assert calls == [1, 2, 3]
    state["display_state"] = "menu"
    namespace["menu_key2"]()
    assert calls == [1, 2, 3, "rotate", "menu"]


def test_rollback_failure_is_reported_and_other_files_are_restored(package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = snapshot(package)
    adapter_path = package / "modules" / "apps" / "channel_utilization.py"
    write = patcher._atomic_write
    def fail_install_and_adapter_cleanup(path, state):
        if path == adapter_path:
            if state.content is None:
                raise OSError("simulated cleanup failure")
            write(path, state)
            raise OSError("simulated install failure")
        write(path, state)
    monkeypatch.setattr(patcher, "_atomic_write", fail_install_and_adapter_cleanup)
    with pytest.raises(patcher.FPMSInstallError, match="recovery failed.*simulated cleanup failure"):
        patcher.install_fpms(package, ADAPTER)
    after = snapshot(package)
    assert after.pop("modules/apps/channel_utilization.py")[0] == ADAPTER.read_bytes()
    assert after == before
