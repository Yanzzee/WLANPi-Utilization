from pathlib import Path

from scripts.patch_wlanpi_fpms import _patch_buttons
from scripts.patch_wlanpi_fpms import _patch_fpms


def test_button_patch_adds_idempotent_page_navigation_handlers(
    tmp_path: Path,
) -> None:
    buttons = tmp_path / "buttons.py"
    buttons.write_text(
        "class Buttons:\n"
        "    def menu_up(self, g_vars, menu):\n"
        "        return 'up'\n"
        "\n"
        "    def menu_down(self, g_vars, menu):\n"
        "        return 'down'\n"
        "\n"
        "    def menu_left(self, g_vars, menu):\n"
        "        if g_vars['display_state'] == 'page':\n"
        "            return 'left'\n",
        encoding="utf-8",
    )

    _patch_buttons(buttons)
    first_patch = buttons.read_text(encoding="utf-8")
    _patch_buttons(buttons)

    assert "g_vars.get('page_up_handler')" in first_patch
    assert "g_vars.get('page_down_handler')" in first_patch
    assert "g_vars.pop('page_exit_handler', None)" in first_patch
    assert buttons.read_text(encoding="utf-8") == first_patch


def test_fpms_patch_overrides_auxiliary_dispatchers_and_is_idempotent(
    tmp_path: Path,
) -> None:
    fpms = tmp_path / "fpms.py"
    fpms.write_text(
        "from .modules.apps.scanner import *\n"
        "\n"
        "def main(g_vars, menu):\n"
        "    def menu_key1():\n"
        "        return 'key1'\n"
        "\n"
        "    def menu_key2():\n"
        "        return 'key2'\n"
        "\n"
        "    def menu_key3():\n"
        "        return 'key3'\n"
        "\n"
        "    # update menu options data structure if we're in non-classic mode\n"
        "    return menu_key1, menu_key2, menu_key3\n",
        encoding="utf-8",
    )

    _patch_fpms(fpms)
    first_patch = fpms.read_text(encoding="utf-8")
    _patch_fpms(fpms)

    assert "g_vars.get('page_key1_handler')" in first_patch
    assert "g_vars.get('page_key2_handler')" in first_patch
    assert "g_vars.get('page_key3_handler')" in first_patch
    assert fpms.read_text(encoding="utf-8") == first_patch

    executable = "\n".join(
        line
        for line in first_patch.splitlines()
        if not line.startswith("from .modules")
    )
    namespace: dict[str, object] = {
        "build_channel_utilization_menu": lambda g_vars: {},
    }
    exec(executable, namespace)
    calls: list[int] = []
    g_vars = {
        "display_state": "page",
        "page_key1_handler": lambda: calls.append(1),
        "page_key2_handler": lambda: calls.append(2),
        "page_key3_handler": lambda: calls.append(3),
    }
    dispatchers = namespace["main"](g_vars, [])

    assert dispatchers[0]() is None
    assert dispatchers[1]() is None
    assert dispatchers[2]() is None
    assert calls == [1, 2, 3]

    g_vars["display_state"] = "menu"
    assert dispatchers[0]() == "key1"
    assert dispatchers[1]() == "key2"
    assert dispatchers[2]() == "key3"
