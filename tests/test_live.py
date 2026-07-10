import pytest

from beacon_live.live import LiveCommandError
from beacon_live.live import build_monitor_setup_commands
from beacon_live.live import build_survey_command
from beacon_live.live import build_tshark_command
from beacon_live.live import channel_to_frequency_mhz
from beacon_live.live import configure_monitor_interface


def test_build_monitor_setup_commands_uses_ht20_channel() -> None:
    assert build_monitor_setup_commands("wlan0", "36") == [
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "monitor"],
        ["ip", "link", "set", "wlan0", "up"],
        ["iw", "dev", "wlan0", "set", "channel", "36", "HT20"],
    ]


def test_build_monitor_setup_commands_can_use_explicit_frequency() -> None:
    assert build_monitor_setup_commands("wlan0", "36", frequency_mhz=5975) == [
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "monitor"],
        ["ip", "link", "set", "wlan0", "up"],
        ["iw", "dev", "wlan0", "set", "freq", "5975", "HT20"],
    ]


def test_build_monitor_setup_commands_maps_6ghz_channel_to_frequency() -> None:
    assert build_monitor_setup_commands("wlan0", "5", band="6") == [
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "monitor"],
        ["ip", "link", "set", "wlan0", "up"],
        ["iw", "dev", "wlan0", "set", "freq", "5975", "HT20"],
    ]


def test_channel_to_frequency_mhz_disambiguates_5_and_6_ghz_channels() -> None:
    assert channel_to_frequency_mhz("149", "5") == 5745
    assert channel_to_frequency_mhz("149", "6") == 6695


def test_channel_to_frequency_mhz_supports_24_ghz_channels() -> None:
    assert channel_to_frequency_mhz("1", "2.4") == 2412
    assert channel_to_frequency_mhz("14", "2.4") == 2484


def test_build_tshark_command_uses_line_buffered_beacon_fields() -> None:
    command = build_tshark_command("wlan9")

    assert command[:4] == ["tshark", "-l", "-i", "wlan9"]
    assert "wlan.fc.type_subtype == 8" in command
    assert _field_args(command) == [
        "frame.time_epoch",
        "wlan.ssid",
        "wlan.bssid",
        "wlan.qbss.cu",
        "wlan.qbss.scount",
        "wlan.qbss.adc",
    ]


def test_build_survey_command() -> None:
    assert build_survey_command("wlan0") == ["iw", "dev", "wlan0", "survey", "dump"]


def test_configure_monitor_interface_stops_on_failed_command() -> None:
    commands_seen: list[list[str]] = []
    failing_command = ["iw", "dev", "wlan0", "set", "type", "monitor"]

    def runner(command: list[str]) -> None:
        commands_seen.append(command)
        if command == failing_command:
            raise LiveCommandError(command, returncode=1, stderr="set type failed")

    with pytest.raises(LiveCommandError) as exc_info:
        configure_monitor_interface("wlan0", "36", command_runner=runner)

    assert exc_info.value.command == failing_command
    assert exc_info.value.stderr == "set type failed"
    assert commands_seen == [
        ["ip", "link", "set", "wlan0", "down"],
        failing_command,
    ]


def _field_args(command: list[str]) -> list[str]:
    fields: list[str] = []
    for index, value in enumerate(command):
        if value == "-e":
            fields.append(command[index + 1])
    return fields
