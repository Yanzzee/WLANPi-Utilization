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
