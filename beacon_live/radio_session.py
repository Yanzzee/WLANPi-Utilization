"""Temporary, exclusive use of an idle Linux radio, with state restoration."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from beacon_live.capture_target import CaptureTarget, parse_iw_interfaces
from beacon_live.channel import ChannelDefinition, parse_iw_interface_channel

LOGGER = logging.getLogger(__name__)
LOCK_DIRECTORY = Path("/run/wlanpi-beacon-live/radios")
_ACTIVE_LOCKS: set[int] = set()


def _close_inherited_locks() -> None:
    # An analysis worker must not keep a parent's radio lock alive after a
    # forced parent exit. Subprocess exec already closes O_CLOEXEC descriptors.
    for fd in _ACTIVE_LOCKS:
        os.close(fd)
    _ACTIVE_LOCKS.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_locks)


class RadioSessionError(ValueError):
    """Radio preparation or restoration could not be completed safely."""


@dataclass(frozen=True)
class InterfaceState:
    target: CaptureTarget
    phy: str
    wdev: str
    ifindex: int
    address: str
    mode: str
    up: bool
    channel: ChannelDefinition | None
    no_ht: bool = False


def parse_interface_state(target: CaptureTarget, info: str, link_json: str) -> InterfaceState:
    """Use the administrative UP flag, rather than monitor operstate UNKNOWN."""
    def field(pattern: str) -> str:
        match = re.search(pattern, info, re.MULTILINE)
        if match is None:
            raise RadioSessionError(f"Incomplete iw identity for {target.label}")
        return match.group(1)

    try:
        links = json.loads(link_json)
        if len(links) != 1 or links[0]["ifname"] != target.interface:
            raise ValueError("link identity mismatch")
        link = links[0]
        phy_number = field(r"^\s*wiphy\s+(\d+)\s*$")
        channel = parse_iw_interface_channel(info)
        if re.search(r"^\s*channel\s", info, re.MULTILINE) and channel is None:
            raise RadioSessionError(f"Cannot preserve the channel definition for {target.label}")
        return InterfaceState(
            target, f"phy{phy_number}",
            field(r"^\s*wdev\s+(0x[0-9a-fA-F]+)\s*$"), int(link["ifindex"]),
            link["address"], field(r"^\s*type\s+(\S+)\s*$"),
            "UP" in link["flags"], channel,
            bool(re.search(r"width:\s*20\s+MHz\s*\(no HT\)", info, re.IGNORECASE)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RadioSessionError(f"Cannot snapshot {target.label}: {error}") from error


def parse_packet_sockets(text: str, ifindices: set[int]) -> set[str]:
    """Socket inodes capturing on this radio (including namespace-wide ALL)."""
    lines = text.splitlines()
    if not lines or lines[0].split() != [
        "sk", "RefCnt", "Type", "Proto", "Iface", "R", "Rmem", "User", "Inode",
    ]:
        raise RadioSessionError("Unrecognized /proc/net/packet header")
    inodes = set()
    for line in lines[1:]:
        fields = line.split()
        try:
            if len(fields) != 9:
                raise ValueError("expected nine columns")
            ifindex, protocol = int(fields[4]), int(fields[3], 16)
            if ifindex in ifindices or (ifindex == 0 and protocol == 3):
                inodes.add(str(int(fields[8])))
        except ValueError as error:
            raise RadioSessionError(f"Unrecognized packet socket entry: {line!r}") from error
    return inodes


def _option_interfaces(argv: list[str]) -> set[str]:
    interfaces = set()
    for index, arg in enumerate(argv):
        if arg in {"-i", "--interface"} and index + 1 < len(argv):
            interfaces.add(argv[index + 1])
        elif arg.startswith("-i") and len(arg) > 2:
            interfaces.add(arg[2:])
        elif arg.startswith("--interface="):
            interfaces.add(arg.partition("=")[2])
    return interfaces


def controller_users(target: CaptureTarget, names: set[str], proc: Path = Path("/proc")) -> list[str]:
    """Find supplicants controlling this radio, including Core's supplicants."""
    ns_path = Path("/run/netns") / target.namespace if target.namespace else proc / "self/ns/net"
    try:
        namespace_inode = ns_path.stat().st_ino
        processes = list(proc.iterdir())
    except OSError as error:
        raise RadioSessionError(f"Cannot inspect radio controllers: {error}") from error
    users = []
    for entry in processes:
        if not entry.name.isdigit():
            continue
        try:
            argv = entry.joinpath("cmdline").read_bytes().decode(errors="replace").strip("\0").split("\0")
            program = Path(argv[0]).name
            if program not in {"wpa_supplicant", "hostapd", "NetworkManager", "iwd"}:
                continue
            if entry.joinpath("ns/net").stat().st_ino != namespace_inode:
                continue
            if program in {"NetworkManager", "iwd"}:
                # Daemon existence alone is not an ownership claim. Their
                # per-interface control is checked below through their APIs.
                continue
            controlled = _option_interfaces(argv)
            if program == "hostapd":
                for argument in argv[1:]:
                    if argument.startswith("-") or not argument.endswith(".conf"):
                        continue
                    config = (
                        entry / "root" / argument.lstrip("/")
                        if argument.startswith("/") else entry / "cwd" / argument
                    )
                    try:
                        text = config.read_text()
                    except OSError as error:
                        raise RadioSessionError(f"Cannot inspect hostapd config {config}: {error}") from error
                    controlled.update(re.findall(r"^\s*(?:interface|bss)\s*=\s*(\S+)\s*$", text, re.MULTILINE))
            if names & controlled:
                users.append(f"{program} (pid {entry.name})")
        except FileNotFoundError:
            continue  # Process exited while /proc was being inspected.
        except OSError as error:
            raise RadioSessionError(f"Cannot inspect controller pid {entry.name}: {error}") from error
    return users


def namespace_has_process(target: CaptureTarget, name: str, proc: Path = Path("/proc")) -> bool:
    path = Path("/run/netns") / target.namespace if target.namespace else proc / "self/ns/net"
    try:
        want = path.stat().st_ino
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.joinpath("comm").read_text().strip() == name and entry.joinpath("ns/net").stat().st_ino == want:
                    return True
            except FileNotFoundError:
                continue
    except OSError as error:
        raise RadioSessionError(f"Cannot inspect {name} in {target.label}: {error}") from error
    return False


def socket_users(inodes: set[str], proc: Path = Path("/proc")) -> list[str]:
    """Best-effort labels; unidentified sockets still block radio use."""
    users = set()
    wanted = {f"socket:[{inode}]" for inode in inodes}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if any(os.readlink(fd) in wanted for fd in entry.joinpath("fd").iterdir()):
                name = entry.joinpath("comm").read_text().strip()
                users.add(f"{name} (pid {entry.name})")
        except OSError:
            continue
    return sorted(users) or [f"packet socket inode {inode}" for inode in sorted(inodes)]


class RadioSession:
    """Hold a phy lock and restore only interface changes made by this session.

    This lock coordinates Utilization processes. Other tools need to cooperate
    with it; preflight checks detect existing external users, not a kernel lease.
    """

    def __init__(
        self, target: CaptureTarget, *,
        tune_builder: Callable[[str, ChannelDefinition], list[str]],
        command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        lock_directory: Path = LOCK_DIRECTORY,
        controllers: Callable[[CaptureTarget, set[str]], list[str]] = controller_users,
        capture_users: Callable[[set[str]], list[str]] = socket_users,
    ) -> None:
        self.target = target
        self.tune_builder = tune_builder
        self.runner = command_runner
        self.lock_directory = lock_directory
        self.controllers = controllers
        self.capture_users = capture_users
        self.states: tuple[InterfaceState, ...] = ()
        self.changed: list[InterfaceState] = []
        self.lock_fd: int | None = None
        self.previous_sigterm = None

    def read(self, command: list[str]) -> str:
        argv = self.target.command(command)
        options = {}
        if command[0] == "nmcli":
            # nmcli's yes/no values are localized unless the locale is fixed.
            options["env"] = {**os.environ, "LC_ALL": "C"}
        try:
            result = self.runner(argv, capture_output=True, text=True, check=False, timeout=3, **options)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RadioSessionError(f"Cannot run {shlex.join(argv)}: {error}") from error
        if result.returncode:
            raise RadioSessionError(f"{shlex.join(argv)} failed: {(result.stderr or '').strip()}")
        return result.stdout

    def snapshot(self, target: CaptureTarget) -> InterfaceState:
        return parse_interface_state(
            target, self.read(["iw", "dev", target.interface, "info"]),
            self.read(["ip", "-j", "link", "show", "dev", target.interface]),
        )

    def __enter__(self) -> RadioSession:
        try:
            import fcntl  # Linux-only functionality stays out of replay imports.

            selected = self.snapshot(self.target)
            self.lock_directory.mkdir(parents=True, exist_ok=True)
            self.lock_fd = os.open(
                self.lock_directory / f"{selected.phy}.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
            )
            _ACTIVE_LOCKS.add(self.lock_fd)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RadioSessionError(f"Radio {selected.phy} is already used by another Utilization session") from error
            # Refresh after acquiring the lock; inventory every sibling VIF.
            selected = self.snapshot(self.target)
            interfaces = parse_iw_interfaces(self.read(["iw", "dev"]), self.target.namespace)
            self.states = tuple(
                self.snapshot(interface.target) for interface in interfaces if interface.phy == selected.phy
            )
            if selected not in self.states:
                raise RadioSessionError(f"Radio inventory changed for {self.target.label}; retry")
            self.check_idle()
            if threading.current_thread() is threading.main_thread():
                self.previous_sigterm = signal.signal(signal.SIGTERM, self._terminate)
            return self
        except (OSError, ImportError) as error:
            self.release()
            raise RadioSessionError(f"Cannot reserve radio {self.target.label}: {error}") from error
        except BaseException:
            self.release()
            raise

    @staticmethod
    def _terminate(signum, frame) -> None:
        raise KeyboardInterrupt

    @property
    def original_mode(self) -> str:
        return next(state.mode for state in self.states if state.target == self.target)

    def check_idle(self) -> None:
        users = []
        names = {state.target.interface for state in self.states}
        for state in self.states:
            if state.mode not in {"monitor", "managed"}:
                users.append(f"{state.target.label} is in {state.mode} mode")
            elif state.mode == "managed":
                link = self.read(["iw", "dev", state.target.interface, "link"]).strip()
                if link != "Not connected.":
                    users.append(f"{state.target.label} has a Wi-Fi connection")
        inodes = parse_packet_sockets(
            self.read(["cat", "/proc/net/packet"]), {state.ifindex for state in self.states},
        )
        if inodes:
            users.extend(self.capture_users(inodes))
        users.extend(self.controllers(self.target, names))
        # Dynamic nl80211 managers may have no -i argument or packet socket.
        # Query ownership instead of stopping a system-wide network service.
        if self._has_process("NetworkManager"):
            # Core's monitor VIF can exist in iw without a NetworkManager
            # device object. Absence from NM's inventory is not an inspection
            # failure; only listed siblings can have an NM ownership flag.
            nm_devices = set(self.read(["nmcli", "-g", "DEVICE", "device", "status"]).splitlines())
            for name in sorted(names & nm_devices):
                managed = self.read(["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", name]).strip()
                if managed not in {"yes", "no"}:
                    raise RadioSessionError(f"Unrecognized NetworkManager ownership for {name}: {managed!r}")
                if managed == "yes":
                    users.append(f"NetworkManager manages {name}")
        if self._has_process("iwd"):
            # iwd uses nl80211 and can scan while disconnected. No safe borrow
            # contract is available here; refuse this namespace conservatively.
            users.append("iwd is running in the radio's namespace")
        if users:
            raise RadioSessionError(
                f"Radio {self.states[0].phy} ({self.target.label}) is busy: {'; '.join(users)}. "
                "Stop/release that radio in the owning tool, or select another adapter."
            )

    def _has_process(self, name: str) -> bool:
        return namespace_has_process(self.target, name)

    def prepare(self) -> None:
        """Pause idle sibling interfaces; their saved identity/state stays intact."""
        # Capability/width negotiation can take time. Check ownership again
        # immediately before the first mutation.
        self.check_idle()
        if any(self.snapshot(state.target) != state for state in self.states):
            raise RadioSessionError("Radio state changed before capture setup; retry")
        for state in self.states:
            if state.target != self.target and state.up:
                self.changed.append(state)  # Include commands interrupted after execution.
                self.read(["ip", "link", "set", state.target.interface, "down"])
        selected = next(state for state in self.states if state.target == self.target)
        self.changed.append(selected)

    def restore(self) -> None:
        errors = []
        for state in reversed(self.changed):
            try:
                current = self.snapshot(state.target)
                if (current.phy, current.wdev, current.ifindex, current.address) != (
                    state.phy, state.wdev, state.ifindex, state.address,
                ):
                    raise RadioSessionError(f"Identity changed for {state.target.label}; refusing to modify replacement")
                name = state.target.interface
                try:
                    if current.mode != state.mode:
                        self.read(["ip", "link", "set", name, "down"])
                        self.read(["iw", "dev", name, "set", "type", state.mode])
                    if state.mode == "monitor" and state.channel is not None:
                        self.read(["ip", "link", "set", name, "up"])
                        tune = self.tune_builder(name, state.channel)
                        if state.no_ht:
                            tune[-1] = "NOHT"
                        self.read(tune)
                finally:
                    # A failed retune must not leave a previously-down VIF up.
                    self.read(["ip", "link", "set", name, "up" if state.up else "down"])
            except (OSError, ValueError) as error:
                errors.append(str(error))
        if errors:
            raise RadioSessionError(f"Radio restoration incomplete: {'; '.join(errors)}")

    def release(self) -> None:
        if self.previous_sigterm is not None:
            signal.signal(signal.SIGTERM, self.previous_sigterm)
            self.previous_sigterm = None
        if self.lock_fd is not None:
            _ACTIVE_LOCKS.discard(self.lock_fd)
            os.close(self.lock_fd)
            self.lock_fd = None
        # Do not unlink locks: another process might already hold the inode.

    def __exit__(self, exc_type, exc, traceback) -> bool:
        old_mask = None
        if threading.current_thread() is threading.main_thread():
            # A second Ctrl+C or SIGTERM must not interrupt restoration. SIGKILL
            # cannot be caught; /run locks are released by the kernel on exit.
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            self.restore()
        except RadioSessionError as error:
            if exc is None:
                raise
            LOGGER.error("%s (while handling %s)", error, exc)
        finally:
            try:
                self.release()
            finally:
                if old_mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        return False
