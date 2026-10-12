import json
import signal
import subprocess
from pathlib import Path

import pytest

from beacon_live.capture_target import CaptureTarget
from beacon_live.live import build_definition_tune_command
from beacon_live.radio_session import (
    RadioSession, RadioSessionError, controller_users,
    parse_interface_state, parse_packet_sockets, namespace_has_process,
)

PACKET_HEADER = "sk RefCnt Type Proto Iface R Rmem User Inode\n"


class RadioHardware:
    """Stateful fake commands: restoration is checked against changed hardware."""

    def __init__(self, namespace=None, mode="monitor", up=True, sibling=True):
        self.namespace = namespace
        self.interfaces = {
            "capture0": dict(mode=mode, up=up, ifindex=6, wdev="0x2", channel=2412),
        }
        if sibling:
            self.interfaces["wifi1"] = dict(mode="managed", up=True, ifindex=4, wdev="0x1", channel=None)
        self.calls = []
        self.packet = PACKET_HEADER
        self.connected = set()
        self.fail = None
        self.failed = False

    def run(self, command, **kwargs):
        self.calls.append(command)
        assert kwargs["timeout"] == 3
        if self.namespace:
            assert command[:4] == ["ip", "netns", "exec", self.namespace]
            command = command[4:]
        else:
            assert command[:3] != ["ip", "netns", "exec"]
        if command == ["iw", "dev"]:
            out = "phy#0\n" + "\n".join(
                f" Interface {name}\n type {state['mode']}" for name, state in self.interfaces.items()
            )
        elif command == ["cat", "/proc/net/packet"]:
            out = self.packet
        elif command[:3] == ["ip", "-j", "link"]:
            name = command[-1]
            state = self.interfaces[name]
            out = json.dumps([dict(
                ifname=name, ifindex=state["ifindex"], address="02:00:00:00:00:01",
                flags=["UP"] if state["up"] else [], operstate="UNKNOWN",
            )])
        elif command[0] == "iw" and command[-1] == "info":
            name = command[2]
            state = self.interfaces[name]
            out = f"Interface {name}\n wiphy 0\n wdev {state['wdev']}\n type {state['mode']}\n"
            if state["channel"]:
                freq = state["channel"]
                width = state.get("width", 20)
                no_ht = " (no HT)" if state.get("no_ht") else ""
                center = state.get("center", freq)
                out += f" channel 1 ({freq} MHz), width: {width} MHz{no_ht}, center1: {center} MHz\n"
        elif command[0] == "iw" and command[-1] == "link":
            out = "Connected to 00:01:02:03:04:05" if command[2] in self.connected else "Not connected.\n"
        else:
            out = ""
            if self.fail and self.fail(command) and not self.failed:
                self.failed = True
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="simulated failure")
            if command[:3] == ["ip", "link", "set"]:
                self.interfaces[command[3]]["up"] = command[4] == "up"
            elif command[:2] == ["iw", "dev"] and command[3:5] == ["set", "type"]:
                self.interfaces[command[2]]["mode"] = command[5]
            elif command[:2] == ["iw", "dev"] and command[3:5] == ["set", "freq"]:
                self.interfaces[command[2]]["channel"] = int(command[5])
            else:
                pytest.fail(f"Unexpected command {command}")
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

    def session(self, tmp_path, **kwargs):
        session = RadioSession(
            CaptureTarget(kwargs.pop("interface", "capture0"), self.namespace), tune_builder=build_definition_tune_command,
            command_runner=self.run, lock_directory=tmp_path / "locks",
            controllers=kwargs.pop("controllers", lambda *args: []),
            capture_users=kwargs.pop("capture_users", lambda inodes: ["dumpcap (pid 123)"]),
            **kwargs,
        )
        session._has_process = lambda name: False
        return session


def test_admin_up_uses_flags_not_operstate_unknown():
    hardware = RadioHardware()
    state = hardware.session(Path("unused")).snapshot(CaptureTarget("capture0"))
    assert state.up and state.mode == "monitor" and state.phy == "phy0"
    with pytest.raises(RadioSessionError, match="Cannot snapshot"):
        parse_interface_state(CaptureTarget("capture0"), "", "[]")


def test_packet_sockets_scope_to_radio_and_namespace_wide_capture():
    text = PACKET_HEADER + (
        "0 3 3 0003 6 1 0 0 100\n"
        "0 3 3 0003 4 1 0 0 101\n"
        "0 3 3 0003 99 1 0 0 102\n"
        "0 3 3 0003 0 1 0 0 103\n"
        "0 3 2 888e 0 1 0 0 104\n"
    )
    assert parse_packet_sockets(text, {4, 6}) == {"100", "101", "103"}
    with pytest.raises(RadioSessionError, match="header"):
        parse_packet_sockets("", {6})
    with pytest.raises(RadioSessionError, match="entry"):
        parse_packet_sockets(PACKET_HEADER + "unexpected layout", {6})


@pytest.mark.parametrize("width,center,no_ht,expected", [
    (20, 2412, True, ["2412", "NOHT"]),
    (80, 5210, False, ["5180", "80", "5210"]),
])
def test_restoration_preserves_non_ht_and_bonded_tuning(tmp_path, width, center, no_ht, expected):
    hardware = RadioHardware(sibling=False)
    hardware.interfaces["capture0"].update(
        channel=2412 if width == 20 else 5180, width=width, center=center, no_ht=no_ht,
    )
    with hardware.session(tmp_path) as session:
        session.prepare()
        hardware.interfaces["capture0"]["channel"] = 2437
    tunes = [command for command in hardware.calls if command[:5] == ["iw", "dev", "capture0", "set", "freq"]]
    assert tunes == [["iw", "dev", "capture0", "set", "freq", *expected]]


@pytest.mark.parametrize("namespace", [None, "capture-lab"])
@pytest.mark.parametrize("mode,up", [("monitor", True), ("monitor", False), ("managed", False), ("managed", True)])
def test_restore_modes_channels_and_siblings_without_removing_interfaces(tmp_path, namespace, mode, up):
    hardware = RadioHardware(namespace, mode, up)
    original = {name: dict(state) for name, state in hardware.interfaces.items()}
    with hardware.session(tmp_path) as session:
        session.prepare()
        assert not hardware.interfaces["wifi1"]["up"]
        session.read(["iw", "dev", "capture0", "set", "type", "monitor"])
        session.read(["ip", "link", "set", "capture0", "up"])
        session.read(["iw", "dev", "capture0", "set", "freq", "5180", "HT20"])
    assert set(hardware.interfaces) == set(original)
    for name, state in original.items():
        actual = hardware.interfaces[name]
        assert actual["mode"] == state["mode"] and actual["up"] == state["up"]
        if state["mode"] == "monitor":
            assert actual["channel"] == state["channel"]
    assert all("del" not in command and "delete" not in command for command in hardware.calls)


@pytest.mark.parametrize("conflict", ["capture", "sibling-capture", "connection", "ap", "supplicant"])
def test_busy_radio_is_not_mutated(tmp_path, conflict):
    hardware = RadioHardware()
    controllers = lambda *args: []
    if "capture" in conflict:
        idx = 4 if conflict == "sibling-capture" else 6
        hardware.packet += f"0 3 3 0003 {idx} 1 0 0 12345\n"
    elif conflict == "connection":
        hardware.connected.add("wifi1")
    elif conflict == "ap":
        hardware.interfaces["wifi1"]["mode"] = "AP"
    elif conflict == "supplicant":
        controllers = lambda *args: ["wpa_supplicant (pid 456)"]
    with pytest.raises(RadioSessionError, match="busy.*Stop/release"):
        with hardware.session(tmp_path, controllers=controllers):
            pytest.fail("must not acquire busy radio")
    assert not any("set" in command for command in hardware.calls)
    # A refused session must release its lock too.
    hardware.packet = PACKET_HEADER
    hardware.connected.clear()
    hardware.interfaces["wifi1"]["mode"] = "managed"
    with hardware.session(tmp_path):
        pass


def test_second_utilization_session_on_same_phy_is_refused(tmp_path):
    hardware = RadioHardware()
    with hardware.session(tmp_path):
        with pytest.raises(RadioSessionError, match="another Utilization"):
            with hardware.session(tmp_path):
                pytest.fail("shared phy lock must fail")
    with hardware.session(tmp_path):
        pass


def test_owner_started_during_capability_checks_is_rejected_before_mutation(tmp_path):
    hardware = RadioHardware()
    with pytest.raises(RadioSessionError, match="busy"):
        with hardware.session(tmp_path) as session:
            hardware.packet += "0 3 3 0003 6 1 0 0 500\n"
            session.prepare()
    assert not any("set" in command for command in hardware.calls)


def test_existing_idle_sibling_monitor_is_preserved(tmp_path):
    hardware = RadioHardware()
    hardware.interfaces["wifi1"]["mode"] = "monitor"
    hardware.interfaces["wifi1"]["channel"] = 5180
    with hardware.session(tmp_path) as session:
        session.prepare()
        assert not hardware.interfaces["wifi1"]["up"]
    assert hardware.interfaces["wifi1"]["up"]
    assert hardware.interfaces["wifi1"]["channel"] == 5180


@pytest.mark.parametrize("manager", ["NetworkManager", "iwd"])
def test_dynamic_network_controllers_prevent_borrowing(tmp_path, manager):
    hardware = RadioHardware()
    original_run = hardware.run
    def run(command, **kwargs):
        if command[0] == "nmcli":
            if command == ["nmcli", "-g", "DEVICE", "device", "status"]:
                return subprocess.CompletedProcess(command, 0, stdout="capture0\nwifi1\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="yes\n", stderr="")
        return original_run(command, **kwargs)
    hardware.run = run
    session = hardware.session(tmp_path)
    session._has_process = lambda name: name == manager
    with pytest.raises(RadioSessionError, match=f"busy.*{manager}"):
        with session:
            pytest.fail("must refuse controller")
    assert not any("set" in command for command in hardware.calls)


@pytest.mark.parametrize("namespace", [None, "capture-lab"])
def test_nm_unlisted_monitor_is_allowed_and_listed_siblings_still_checked(
    tmp_path, monkeypatch, namespace,
):
    hardware = RadioHardware(namespace)
    hardware.interfaces["wlanpi0"] = hardware.interfaces.pop("capture0")
    original_run = hardware.run
    nm_commands = []
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")

    def run(command, **kwargs):
        native = command[4:] if namespace else command
        if native[0] == "nmcli":
            assert kwargs["env"]["LC_ALL"] == "C"
            if namespace:
                assert command[:4] == ["ip", "netns", "exec", namespace]
            nm_commands.append(native)
            if native == ["nmcli", "-g", "DEVICE", "device", "status"]:
                # NetworkManager sees the managed sibling and unrelated devices,
                # but not the monitor interface discovered through iw.
                return subprocess.CompletedProcess(command, 0, stdout="wifi1\neth0\nlo\n", stderr="")
            if native[-1] == "wlanpi0":
                return subprocess.CompletedProcess(command, 10, stdout="", stderr="Error: Device 'wlanpi0' not found.")
            assert native == ["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wifi1"]
            return subprocess.CompletedProcess(command, 0, stdout="no\n", stderr="")
        return original_run(command, **kwargs)

    hardware.run = run
    session = hardware.session(tmp_path, interface="wlanpi0")
    session._has_process = lambda name: name == "NetworkManager"
    with session:
        session.prepare()
    assert hardware.interfaces["wifi1"]["up"]
    assert not any(command[-1] == "wlanpi0" for command in nm_commands)
    assert ["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wifi1"] in nm_commands


@pytest.mark.parametrize("managed_interface", ["capture0", "wifi1"])
def test_nm_listed_managed_radio_interface_is_still_refused(tmp_path, managed_interface):
    hardware = RadioHardware()
    original_run = hardware.run
    def run(command, **kwargs):
        if command == ["nmcli", "-g", "DEVICE", "device", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{managed_interface}\n", stderr="")
        if command[0] == "nmcli":
            assert command[-1] == managed_interface
            return subprocess.CompletedProcess(command, 0, stdout="yes\n", stderr="")
        return original_run(command, **kwargs)
    hardware.run = run
    session = hardware.session(tmp_path)
    session._has_process = lambda name: name == "NetworkManager"
    with pytest.raises(RadioSessionError, match=f"NetworkManager manages {managed_interface}"):
        with session:
            pytest.fail("must not borrow a managed radio")
    assert not any("set" in command for command in hardware.calls)


@pytest.mark.parametrize("failure_stage", ["inventory", "ownership", "unknown-value"])
def test_nm_inspection_failures_are_not_treated_as_unmanaged(tmp_path, failure_stage):
    hardware = RadioHardware()
    original_run = hardware.run
    def run(command, **kwargs):
        if command[0] == "nmcli":
            if command == ["nmcli", "-g", "DEVICE", "device", "status"]:
                if failure_stage == "inventory":
                    return subprocess.CompletedProcess(command, 8, stdout="", stderr="NetworkManager is not running")
                return subprocess.CompletedProcess(command, 0, stdout="capture0\n", stderr="")
            if failure_stage == "unknown-value":
                return subprocess.CompletedProcess(command, 0, stdout="unexpected\n", stderr="")
            return subprocess.CompletedProcess(command, 10, stdout="", stderr="Device disappeared")
        return original_run(command, **kwargs)
    hardware.run = run
    session = hardware.session(tmp_path)
    session._has_process = lambda name: name == "NetworkManager"
    with pytest.raises(RadioSessionError):
        with session:
            pytest.fail("must not bypass failed ownership checks")
    assert not any("set" in command for command in hardware.calls)


@pytest.mark.parametrize("failure", [RuntimeError("startup failed"), KeyboardInterrupt()])
def test_failed_startup_and_interrupt_restore_changed_state(tmp_path, failure):
    hardware = RadioHardware(mode="managed", up=False)
    with pytest.raises(type(failure)):
        with hardware.session(tmp_path) as session:
            session.prepare()
            session.read(["iw", "dev", "capture0", "set", "type", "monitor"])
            session.read(["ip", "link", "set", "capture0", "up"])
            raise failure
    assert hardware.interfaces["capture0"]["mode"] == "managed"
    assert not hardware.interfaces["capture0"]["up"]
    assert hardware.interfaces["wifi1"]["up"]


def test_partial_prepare_failure_restores_only_changed_sibling(tmp_path):
    hardware = RadioHardware()
    hardware.fail = lambda command: command == ["ip", "link", "set", "wifi1", "down"]
    with pytest.raises(RadioSessionError, match="simulated failure"):
        with hardware.session(tmp_path) as session:
            session.prepare()
    assert hardware.interfaces["wifi1"]["up"]
    assert hardware.interfaces["capture0"]["channel"] == 2412


def test_replacement_interface_is_not_modified_on_exit(tmp_path):
    hardware = RadioHardware(sibling=False)
    with pytest.raises(RadioSessionError, match="Identity changed"):
        with hardware.session(tmp_path) as session:
            session.prepare()
            hardware.interfaces["capture0"]["wdev"] = "0xabcd"
            hardware.interfaces["capture0"]["channel"] = 5180
    assert hardware.interfaces["capture0"]["channel"] == 5180
    assert not any("set" in command for command in hardware.calls)


def test_failed_restoration_retune_still_restores_down_flag_and_other_sibling(tmp_path):
    hardware = RadioHardware(up=False)
    hardware.fail = lambda command: command[:5] == ["iw", "dev", "capture0", "set", "freq"]
    with pytest.raises(RadioSessionError, match="restoration incomplete"):
        with hardware.session(tmp_path) as session:
            session.prepare()
            hardware.interfaces["capture0"]["channel"] = 5180
    assert not hardware.interfaces["capture0"]["up"]
    assert hardware.interfaces["wifi1"]["up"]


def test_sigterm_handler_restores_state_and_original_handler(tmp_path):
    hardware = RadioHardware(mode="managed", up=False)
    original_handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        with hardware.session(tmp_path) as session:
            session.prepare()
            hardware.interfaces["capture0"]["mode"] = "monitor"
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
    assert signal.getsignal(signal.SIGTERM) == original_handler
    assert hardware.interfaces["capture0"]["mode"] == "managed"


def test_controller_detection_scopes_process_namespace_and_interface(tmp_path):
    proc = tmp_path / "proc"
    (proc / "self/ns").mkdir(parents=True)
    (proc / "self/ns/net").write_text("namespace")
    for pid, argv, same_namespace in [
        (10, ["/sbin/wpa_supplicant", "-iwifi1"], True),
        (11, ["/sbin/wpa_supplicant", "-i", "wifi1"], False),
        (12, ["/sbin/wpa_supplicant", "-i", "other-radio"], True),
        (13, ["/usr/bin/python", "app.py"], True),
    ]:
        entry = proc / str(pid)
        (entry / "ns").mkdir(parents=True)
        (entry / "cmdline").write_bytes("\0".join(argv).encode())
        (entry / "comm").write_text(Path(argv[0]).name)
        if same_namespace:
            (entry / "ns/net").symlink_to(proc / "self/ns/net")
        else:
            (entry / "ns/net").write_text("other namespace")
    assert controller_users(CaptureTarget("capture0"), {"capture0", "wifi1"}, proc) == [
        "wpa_supplicant (pid 10)",
    ]
    assert namespace_has_process(CaptureTarget("capture0"), "wpa_supplicant", proc)
    assert not namespace_has_process(CaptureTarget("capture0"), "iwd", proc)
