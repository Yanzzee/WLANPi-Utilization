from pathlib import Path

from scripts.patch_wlanpi_fpms import _patch_buttons


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
        "            return 'left'\n"
        "\n"
        "    def menu_key1(self, g_vars, menu):\n"
        "        return 'key1'\n"
        "\n"
        "    def menu_key2(self, g_vars, menu):\n"
        "        return 'key2'\n"
        "\n"
        "    def menu_key3(self, g_vars, menu):\n"
        "        return 'key3'\n",
        encoding="utf-8",
    )

    _patch_buttons(buttons)
    first_patch = buttons.read_text(encoding="utf-8")
    _patch_buttons(buttons)

    assert "g_vars.get('page_up_handler')" in first_patch
    assert "g_vars.get('page_down_handler')" in first_patch
    assert "g_vars.pop('page_exit_handler', None)" in first_patch
    assert "g_vars.get('page_key1_handler')" in first_patch
    assert "g_vars.get('page_key2_handler')" in first_patch
    assert "g_vars.get('page_key3_handler')" in first_patch
    assert buttons.read_text(encoding="utf-8") == first_patch

    namespace: dict[str, object] = {}
    exec(first_patch, namespace)
    patched_buttons = namespace["Buttons"]()
    calls: list[int] = []
    g_vars = {
        "display_state": "page",
        "page_key1_handler": lambda: calls.append(1),
        "page_key2_handler": lambda: calls.append(2),
        "page_key3_handler": lambda: calls.append(3),
    }

    assert patched_buttons.menu_key1(g_vars, None) is None
    assert patched_buttons.menu_key2(g_vars, None) is None
    assert patched_buttons.menu_key3(g_vars, None) is None
    assert calls == [1, 2, 3]

    g_vars["display_state"] = "menu"
    assert patched_buttons.menu_key1(g_vars, None) == "key1"
    assert patched_buttons.menu_key2(g_vars, None) == "key2"
    assert patched_buttons.menu_key3(g_vars, None) == "key3"
