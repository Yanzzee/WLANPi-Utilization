"""Read-only radio discovery and command context for Linux capture."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable

from beacon_live.channel import parse_iw_phy_capabilities

LOGGER = logging.getLogger(__name__)


class CaptureTargetError(ValueError):
    """A suitable, unambiguous capture interface could not be found."""


@dataclass(frozen=True)
class CaptureTarget:
    interface: str
    namespace: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", self.interface):
            raise CaptureTargetError(f"Invalid interface name: {self.interface!r}")
        if self.namespace is not None and not re.fullmatch(
            r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.namespace
        ):
            raise CaptureTargetError(f"Invalid network namespace: {self.namespace!r}")

    @property
    def label(self) -> str:
        return f"{self.namespace or 'root'}:{self.interface}"

    def command(self, argv: list[str]) -> list[str]:
        if self.namespace is None:
            return list(argv)
        return ["ip", "netns", "exec", self.namespace, *argv]


def as_capture_target(interface: str | CaptureTarget) -> CaptureTarget:
    return interface if isinstance(interface, CaptureTarget) else CaptureTarget(interface)


@dataclass(frozen=True)
class WirelessInterface:
    target: CaptureTarget
    phy: str
    mode: str


def parse_iw_interfaces(text: str, namespace: str | None = None) -> tuple[WirelessInterface, ...]:
    """Parse iw dev inventory, preserving the phy shared by virtual interfaces."""
    interfaces = []
    phy = None
    name = None
    mode = None

    def finish() -> None:
        if phy is not None and name is not None and mode is not None:
            interfaces.append(WirelessInterface(CaptureTarget(name, namespace), phy, mode))

    for line in text.splitlines():
        stripped = line.strip()
        phy_match = re.fullmatch(r"phy#(\d+)", stripped)
        if phy_match:
            finish()
            phy, name, mode = f"phy{phy_match.group(1)}", None, None
        elif stripped.startswith("Interface "):
            finish()
            name, mode = stripped.removeprefix("Interface ").strip(), None
        elif "non-netdev interface" in stripped:
            finish()
            name, mode = None, None
        elif stripped.startswith("type "):
            mode = stripped.removeprefix("type ").strip()
    finish()
    return tuple(interfaces)


def parse_namespace_names(text: str) -> tuple[str, ...]:
    names = []
    for line in text.splitlines():
        match = re.fullmatch(r"([^\s]+)(?:\s+\(id:\s*\d+\))?", line.strip())
        if match:
            name = match.group(1)
            CaptureTarget("probe", name)
            names.append(name)
        elif line.strip():
            raise CaptureTargetError(f"Unrecognized ip netns list entry: {line!r}")
    return tuple(sorted(set(names)))


def discover_capture_target(
    frequency_mhz: int,
    *,
    namespace: str | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> CaptureTarget:
    """Prefer existing monitors; require an explicit choice when equally suitable."""

    def read(command: list[str]) -> str:
        try:
            result = command_runner(
                command, capture_output=True, text=True, check=False, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CaptureTargetError(f"Cannot inspect {shlex.join(command)}: {error}") from error
        if result.returncode != 0:
            raise CaptureTargetError(
                f"Cannot inspect {shlex.join(command)} (status {result.returncode}): "
                f"{(result.stderr or '').strip()}"
            )
        return result.stdout

    if namespace is not None:
        CaptureTarget("probe", namespace)
        contexts = [namespace]
    else:
        contexts = [None, *parse_namespace_names(read(["ip", "netns", "list"]))]
    candidates = []
    failures = []
    for context in contexts:
        probe = CaptureTarget("probe", context)
        try:
            interfaces = parse_iw_interfaces(read(probe.command(["iw", "dev"])), context)
        except CaptureTargetError as error:
            failures.append(str(error))
            LOGGER.warning("%s", error)
            continue
        capabilities = {}
        for interface in interfaces:
            if interface.mode not in {"monitor", "managed"}:
                continue
            if interface.phy not in capabilities:
                try:
                    capabilities[interface.phy] = parse_iw_phy_capabilities(read(
                        probe.command(["iw", "phy", interface.phy, "info"])
                    ))
                except CaptureTargetError as error:
                    failures.append(str(error))
                    LOGGER.warning("%s", error)
                    capabilities[interface.phy] = None
            capability = capabilities[interface.phy]
            if (
                capability is not None and capability.supports_monitor
                and frequency_mhz in capability.supported_frequencies_mhz
            ):
                candidates.append(interface)
    monitors = [interface for interface in candidates if interface.mode == "monitor"]
    preferred = monitors or candidates
    if not preferred:
        detail = f" Inspection errors: {'; '.join(failures)}" if failures else ""
        raise CaptureTargetError(
            f"No monitor-capable capture interface supports {frequency_mhz} MHz"
            f"{f' in namespace {namespace}' if namespace else ''}. "
            f"Check iw dev, iw phy, and ip netns list.{detail}"
        )
    if len(preferred) != 1:
        choices = ", ".join(sorted(interface.target.label for interface in preferred))
        raise CaptureTargetError(
            f"Multiple capture interfaces support {frequency_mhz} MHz: {choices}. "
            "Choose --iface and, for a named namespace, --netns."
        )
    return preferred[0].target
