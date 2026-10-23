import subprocess

import pytest

from beacon_live.capture_target import (
    CaptureTarget, CaptureTargetError, discover_capture_target,
    parse_iw_interfaces, parse_namespace_names,
    list_wireless_interfaces,
)


def inventory(*interfaces: tuple[str, str, int]) -> str:
    return "\n".join(
        f"phy#{phy}\n\tInterface {name}\n\t\ttype {mode}"
        for name, mode, phy in interfaces
    )


def capabilities(*frequencies: int, monitor: bool = True) -> str:
    modes = "* managed\n* monitor" if monitor else "* managed"
    return f"Supported interface modes:\n{modes}\nBand 1:\n" + "\n".join(
        f"* {frequency}.0 MHz [1] (20 dBm)" for frequency in frequencies
    )


def runner_for(outputs: dict[tuple[str, ...], str | int]):
    calls = []
    def run(command, **kwargs):
        assert kwargs["timeout"] == 3
        calls.append(command)
        value = outputs[tuple(command)]
        return subprocess.CompletedProcess(
            command, value if isinstance(value, int) else 0,
            stdout="" if isinstance(value, int) else value,
            stderr="namespace unavailable" if isinstance(value, int) else "",
        )
    return run, calls


def test_interface_inventory_is_read_only_and_includes_all_modes_and_namespaces():
    calls = []
    responses = {("iw", "dev"): inventory(("wlan0", "managed", 0), ("mon0", "monitor", 0)),
                 ("ip", "netns", "list"): "lab\n",
                 ("ip", "netns", "exec", "lab", "iw", "dev"): inventory(("wlan0", "AP", 1))}
    def run(command, **kwargs):
        calls.append(command)
        assert 0 < kwargs["timeout"] <= 1
        return subprocess.CompletedProcess(command, 0, responses[tuple(command)], "")
    result = list_wireless_interfaces(command_runner=run)
    assert {item.target.label for item in result.interfaces} == {"root:wlan0", "root:mon0", "lab:wlan0"}
    assert {item.mode for item in result.interfaces} == {"managed", "monitor", "AP"}
    assert not result.errors
    assert len(calls) == 3 and all("set" not in call for call in calls)


def test_interface_inventory_retains_root_results_when_namespace_listing_fails():
    def run(command, **kwargs):
        if command[0] == "iw":
            return subprocess.CompletedProcess(command, 0, inventory(("sniffer", "monitor", 1)), "")
        raise FileNotFoundError("ip unavailable")
    result = list_wireless_interfaces(command_runner=run)
    assert result.interfaces[0].target == CaptureTarget("sniffer")
    assert result.errors


def test_interface_inventory_has_an_overall_deadline_and_partial_results():
    now, calls = [0.0], []
    def run(command, **kwargs):
        calls.append(command)
        now[0] += 1
        text = "\n".join(f"ns{i}" for i in range(10)) if command[:3] == ["ip", "netns", "list"] else inventory(("sniffer", "monitor", 1))
        return subprocess.CompletedProcess(command, 0, text, "")
    result = list_wireless_interfaces(command_runner=run, clock=lambda: now[0])
    assert len(calls) == 5
    assert len(result.interfaces) == 4  # root plus three inspected namespaces
    assert "timed out" in result.errors[-1]


def test_parse_iw_dev_keeps_namespace_and_shared_phy() -> None:
    records = parse_iw_interfaces(
        "phy#2\n Interface renamed\n type managed\n Interface mon-a\n type monitor\n"
        "phy#4\n Interface wlan7\n type managed\n", "lab"
    )
    assert [(record.target, record.phy, record.mode) for record in records] == [
        (CaptureTarget("renamed", "lab"), "phy2", "managed"),
        (CaptureTarget("mon-a", "lab"), "phy2", "monitor"),
        (CaptureTarget("wlan7", "lab"), "phy4", "managed"),
    ]
    assert parse_iw_interfaces("") == ()


def test_namespace_names_and_argv_context() -> None:
    assert parse_namespace_names("lab (id: 0)\nscan-2\nlab (id: 0)\n") == ("lab", "scan-2")
    command = ["iw", "dev", "renamed", "info"]
    assert CaptureTarget("renamed").command(command) == command
    assert CaptureTarget("renamed", "lab").command(command) == ["ip", "netns", "exec", "lab", *command]
    assert command == ["iw", "dev", "renamed", "info"]


def test_unnamed_p2p_device_does_not_overwrite_previous_interface() -> None:
    records = parse_iw_interfaces(
        "phy#0\n Interface mon0\n type monitor\n"
        " Unnamed/non-netdev interface\n wdev 0x2\n type P2P-device\n"
    )
    assert [(record.target.interface, record.mode) for record in records] == [("mon0", "monitor")]


def test_discovery_prefers_existing_monitor_and_queries_phy_once() -> None:
    run, calls = runner_for({
        ("ip", "netns", "list"): "",
        ("iw", "dev"): inventory(("wlan0", "managed", 1), ("renamed", "managed", 0), ("sniffer", "monitor", 0)),
        ("iw", "phy", "phy0", "info"): capabilities(2412, 5180, 5975),
        ("iw", "phy", "phy1", "info"): capabilities(2412, monitor=False),
    })
    assert discover_capture_target(2412, command_runner=run) == CaptureTarget("sniffer")
    assert calls.count(["iw", "phy", "phy0", "info"]) == 1
    assert not any("set" in command for command in calls)


def test_discovery_filters_by_frequency_before_selecting_monitor() -> None:
    run, _ = runner_for({
        ("ip", "netns", "list"): "",
        ("iw", "dev"): inventory(("mon-24", "monitor", 0), ("wifi6", "managed", 1)),
        ("iw", "phy", "phy0", "info"): capabilities(2412),
        ("iw", "phy", "phy1", "info"): capabilities(5975),
    })
    assert discover_capture_target(5975, command_runner=run) == CaptureTarget("wifi6")


def test_discovery_finds_monitor_in_named_namespace() -> None:
    run, calls = runner_for({
        ("ip", "netns", "list"): "lab (id: 1)\n",
        ("iw", "dev"): inventory(("wlan0", "managed", 0)),
        ("iw", "phy", "phy0", "info"): capabilities(2412, monitor=False),
        ("ip", "netns", "exec", "lab", "iw", "dev"): inventory(("capture0", "monitor", 3)),
        ("ip", "netns", "exec", "lab", "iw", "phy", "phy3", "info"): capabilities(2412),
    })
    assert discover_capture_target(2412, command_runner=run) == CaptureTarget("capture0", "lab")
    assert not any("set" in command for command in calls)


def test_explicit_namespace_limits_discovery() -> None:
    run, calls = runner_for({
        ("ip", "netns", "exec", "lab", "iw", "dev"): inventory(("mon0", "monitor", 0)),
        ("ip", "netns", "exec", "lab", "iw", "phy", "phy0", "info"): capabilities(2412),
    })
    assert discover_capture_target(2412, namespace="lab", command_runner=run) == CaptureTarget("mon0", "lab")
    assert all(command[:4] == ["ip", "netns", "exec", "lab"] for command in calls)


def test_duplicate_names_in_different_namespaces_are_ambiguous() -> None:
    run, _ = runner_for({
        ("ip", "netns", "list"): "lab\n",
        ("iw", "dev"): inventory(("mon0", "monitor", 0)),
        ("iw", "phy", "phy0", "info"): capabilities(2412),
        ("ip", "netns", "exec", "lab", "iw", "dev"): inventory(("mon0", "monitor", 0)),
        ("ip", "netns", "exec", "lab", "iw", "phy", "phy0", "info"): capabilities(2412),
    })
    with pytest.raises(CaptureTargetError, match="lab:mon0, root:mon0.*--iface.*--netns"):
        discover_capture_target(2412, command_runner=run)


def test_multiple_adapters_require_explicit_choice() -> None:
    run, _ = runner_for({
        ("ip", "netns", "list"): "",
        ("iw", "dev"): inventory(("mon-a", "monitor", 0), ("mon-b", "monitor", 1)),
        ("iw", "phy", "phy0", "info"): capabilities(2412),
        ("iw", "phy", "phy1", "info"): capabilities(2412),
    })
    with pytest.raises(CaptureTargetError, match="root:mon-a, root:mon-b"):
        discover_capture_target(2412, command_runner=run)


@pytest.mark.parametrize("phy_info", [
    capabilities(2412, monitor=False),
    capabilities(2412) + "\n* 5975.0 MHz [5] (disabled)",
])
def test_no_suitable_radio_has_actionable_error(phy_info: str) -> None:
    run, _ = runner_for({
        ("ip", "netns", "list"): "",
        ("iw", "dev"): inventory(("wlan0", "managed", 0)),
        ("iw", "phy", "phy0", "info"): phy_info,
    })
    with pytest.raises(CaptureTargetError, match="No monitor-capable capture interface supports 5975 MHz"):
        discover_capture_target(5975, command_runner=run)


def test_unavailable_namespace_reports_inspection_failure() -> None:
    run, _ = runner_for({("ip", "netns", "exec", "missing", "iw", "dev"): 1})
    with pytest.raises(CaptureTargetError, match="namespace missing.*namespace unavailable"):
        discover_capture_target(2412, namespace="missing", command_runner=run)


def test_discovery_skips_failed_namespace_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    run, _ = runner_for({
        ("ip", "netns", "list"): "stale\n",
        ("iw", "dev"): inventory(("mon0", "monitor", 0)),
        ("iw", "phy", "phy0", "info"): capabilities(2412),
        ("ip", "netns", "exec", "stale", "iw", "dev"): 1,
    })
    assert discover_capture_target(2412, command_runner=run) == CaptureTarget("mon0")
    assert "stale" in caplog.text and "namespace unavailable" in caplog.text


@pytest.mark.parametrize("error", [FileNotFoundError("iw"), subprocess.TimeoutExpired(["iw"], 3)])
def test_missing_commands_and_timeouts_are_reported(error: Exception) -> None:
    def run(command, **kwargs):
        raise error
    with pytest.raises(CaptureTargetError, match="Cannot inspect"):
        discover_capture_target(2412, command_runner=run)
