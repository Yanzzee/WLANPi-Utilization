"""Minimal live WLAN frame capture loop."""

from __future__ import annotations

import json
import re
import selectors
import subprocess
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional, Protocol

from beacon_live.analyzer import Analyzer
from beacon_live.channel import ChannelDefinition
from beacon_live.channel import ChannelWidth
from beacon_live.channel import CoverageDecision
from beacon_live.channel import RadioCapabilities
from beacon_live.channel import channel_to_frequency
from beacon_live.channel import frequency_to_channel
from beacon_live.channel import parse_iw_interface_channel
from beacon_live.channel import parse_iw_phy_capabilities
from beacon_live.dashboard import TerminalDashboard
from beacon_live.lcd_dashboard import LcdDashboard
from beacon_live.log_writer import DEFAULT_DISK_CHECK_INTERVAL_SECONDS
from beacon_live.log_writer import DEFAULT_MIN_FREE_BYTES
from beacon_live.log_writer import DEFAULT_ROTATION_INTERVAL_SECONDS
from beacon_live.log_writer import LiveLogPaths
from beacon_live.log_writer import LogMetadata
from beacon_live.log_writer import LoggingEvent
from beacon_live.log_writer import LoggingService
from beacon_live.models import MetricsSnapshot
from beacon_live.models import SecondStats
from beacon_live.models import SurveySample
from beacon_live.parser import parse_tshark_capture_record
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
from beacon_live.parser import TSHARK_LIVE_OPTIONAL_FRAME_FIELD_NAMES
from beacon_live.parser import TSHARK_MULTI_VALUE_SEPARATOR
from beacon_live.parser import TSHARK_OPTIONAL_FRAME_FIELD_NAMES
from beacon_live.survey import SurveyCuResult
from beacon_live.survey import compute_local_cu_result_from_samples
from beacon_live.survey import parse_survey_dump
from beacon_live.tui import CursesDashboard
from beacon_live.tui import should_use_curses

TSHARK_CAPTURE_PROTOCOLS = (
    "radiotap,wlan_radio,wlan,wlan_ext,wlan_aggregate"
)
TSHARK_CAPTURE_BUFFER_MIB = 16
LIVE_SNAPSHOT_LENGTH = 512
LIVE_DISPLAY_FILTER = "wlan"

SUPPORTED_BANDS = {"2.4", "5", "6"}
LIVE_WARMUP_CYCLES = 1
LOGGING_CONTROL_POLL_SECONDS = 0.5
CHANNEL_DEFINITION_MAX_AGE_SECONDS = 10.0
DEFAULT_5_GHZ_80MHZ_CENTER_CHANNELS = (42, 58, 106, 122, 138, 155, 171)
DEFAULT_6_GHZ_80MHZ_CENTER_CHANNELS = tuple(range(7, 216, 16))


class LiveDashboard(Protocol):
    def refresh(self, snapshot: Optional[MetricsSnapshot] = None) -> None:
        ...

    def poll_input(self) -> bool:
        ...


class NullDashboard:
    """Disable rendering while retaining the normal live analyzer path."""

    def refresh(self, snapshot: Optional[MetricsSnapshot] = None) -> None:
        return None

    def poll_input(self) -> bool:
        return False


@dataclass(frozen=True)
class LiveCommandError(Exception):
    command: list[str]
    returncode: Optional[int] = None
    stderr: str = ""

    @property
    def command_text(self) -> str:
        return " ".join(self.command)

    def __str__(self) -> str:
        if self.stderr:
            return self.stderr
        if self.returncode is not None:
            return f"{self.command_text} exited with status {self.returncode}"
        return self.command_text


@dataclass
class LiveWarmupFilter:
    """Drop complete per-second stats from initial live refresh cycles."""

    remaining_cycles: int = LIVE_WARMUP_CYCLES

    def filter(self, stats_rows: list[SecondStats]) -> list[SecondStats]:
        if self.remaining_cycles > 0:
            if not stats_rows:
                return []
            self.remaining_cycles -= 1
            return []
        return stats_rows


def channel_to_frequency_mhz(channel: str, band: str) -> int:
    try:
        channel_number = int(channel)
    except ValueError as exc:
        raise ValueError(f"channel must be an integer, got {channel!r}") from exc

    if channel_number <= 0:
        raise ValueError(f"channel must be greater than zero, got {channel!r}")

    if band == "2.4":
        if channel_number == 14:
            return 2484
        if 1 <= channel_number <= 13:
            return 2407 + (channel_number * 5)
        raise ValueError("2.4 GHz channel must be in the range 1-14")

    if band == "5":
        frequency_mhz = 5000 + (channel_number * 5)
        if 5000 < frequency_mhz < 5925:
            return frequency_mhz
        raise ValueError(
            f"5 GHz channel {channel_number} maps outside the 5 GHz band"
        )

    if band == "6":
        frequency_mhz = 5950 + (channel_number * 5)
        if 5925 <= frequency_mhz <= 7125:
            return frequency_mhz
        raise ValueError(
            f"6 GHz channel {channel_number} maps outside the 6 GHz band"
        )

    raise ValueError(
        f"band must be one of {', '.join(sorted(SUPPORTED_BANDS))}, got {band!r}"
    )


def build_tune_command(
    iface: str,
    channel: str,
    *,
    frequency_mhz: Optional[int] = None,
    band: Optional[str] = None,
) -> list[str]:
    if frequency_mhz is not None:
        if frequency_mhz <= 0:
            raise ValueError(
                f"frequency_mhz must be greater than zero, got {frequency_mhz}"
            )
        return ["iw", "dev", iface, "set", "freq", str(frequency_mhz), "HT20"]

    if band is not None:
        return [
            "iw",
            "dev",
            iface,
            "set",
            "freq",
            str(channel_to_frequency_mhz(channel, band)),
            "HT20",
        ]

    return ["iw", "dev", iface, "set", "channel", channel, "HT20"]


def build_definition_tune_command(
    iface: str,
    definition: ChannelDefinition,
) -> list[str]:
    """Build one explicit nl80211 channel-definition request."""
    command = [
        "iw",
        "dev",
        iface,
        "set",
        "freq",
        str(definition.primary_frequency_mhz),
    ]
    if definition.width is ChannelWidth.MHZ20:
        return command + ["HT20"]
    if definition.width is ChannelWidth.MHZ40:
        offset = (
            "+"
            if (definition.center_frequency1_mhz or 0)
            > definition.primary_frequency_mhz
            else "-"
        )
        command.append(f"HT40{offset}")
        return command
    width_argument = {
        ChannelWidth.MHZ80: "80",
        ChannelWidth.MHZ160: "160",
        ChannelWidth.MHZ80P80: "80+80",
        ChannelWidth.MHZ320: "320",
    }[definition.width]
    command.extend([width_argument, str(definition.center_frequency1_mhz)])
    if definition.width is ChannelWidth.MHZ80P80:
        if definition.center_frequency2_mhz is None:
            raise ValueError("80+80 MHz requires center_frequency2_mhz")
        command.append(str(definition.center_frequency2_mhz))
    if definition.puncturing_bitmap is not None:
        command.extend(["punct", str(definition.puncturing_bitmap)])
    return command


def resolve_survey_target_frequency_mhz(
    channel: str,
    *,
    frequency_mhz: Optional[int] = None,
    band: Optional[str] = None,
) -> Optional[int]:
    if frequency_mhz is not None:
        return frequency_mhz
    if band is not None:
        return channel_to_frequency_mhz(channel, band)

    try:
        channel_number = int(channel)
    except ValueError:
        return None

    if 1 <= channel_number <= 14:
        return channel_to_frequency_mhz(channel, "2.4")

    frequency = 5000 + (channel_number * 5)
    if 5000 < frequency < 5925:
        return frequency

    return None


def frequency_to_band(frequency_mhz: Optional[int]) -> Optional[str]:
    if frequency_mhz is None:
        return None
    if 2400 <= frequency_mhz <= 2500:
        return "2.4"
    if 5000 < frequency_mhz < 5925:
        return "5"
    if 5925 <= frequency_mhz <= 7125:
        return "6"
    return None


def build_monitor_setup_commands(
    iface: str,
    channel: str,
    *,
    frequency_mhz: Optional[int] = None,
    band: Optional[str] = None,
    channel_definition: Optional[ChannelDefinition] = None,
) -> list[list[str]]:
    return [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
        (
            build_definition_tune_command(iface, channel_definition)
            if channel_definition is not None
            else build_tune_command(
                iface,
                channel,
                frequency_mhz=frequency_mhz,
                band=band,
            )
        ),
    ]


def build_tshark_command(
    iface: str,
    *,
    field_names: tuple[str, ...] = TSHARK_FRAME_FIELD_NAMES,
    raw_capture_path: Optional[Path] = None,
) -> list[str]:
    command = [
        "tshark",
        "-l",
        "-i",
        iface,
        "-B",
        str(TSHARK_CAPTURE_BUFFER_MIB),
        "-s",
        "0" if raw_capture_path is not None else str(LIVE_SNAPSHOT_LENGTH),
        "--disable-protocol",
        "ALL",
        "--enable-protocol",
        TSHARK_CAPTURE_PROTOCOLS,
        "-o",
        "wlan.defragment:FALSE",
        "-o",
        "wlan.enable_decryption:FALSE",
        "-N",
        "m",
        "-T",
        "fields",
        "-E",
        "separator=\t",
        "-E",
        "occurrence=a",
        "-E",
        f"aggregator={TSHARK_MULTI_VALUE_SEPARATOR}",
    ]
    if raw_capture_path is None:
        command[command.index("-T"):command.index("-T")] = [
            "-Y",
            LIVE_DISPLAY_FILTER,
        ]
    else:
        # -P keeps the field stream on stdout while -w saves the exact packets
        # from this same capture process. Live display filters cannot be used
        # while writing raw packets, so non-WLAN rows are rejected by parser.
        command.extend(["-P", "-F", "pcapng", "-w", str(raw_capture_path)])
    for field in field_names:
        command.extend(["-e", field])
    return command


def available_tshark_fields(
    *,
    tshark: str = "tshark",
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> frozenset[str]:
    try:
        result = command_runner(
            [tshark, "-G", "fields"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        columns[2]
        for line in result.stdout.splitlines()
        for columns in (line.split("\t"),)
        if len(columns) >= 3 and columns[0] == "F"
    )


def select_tshark_frame_fields(
    available_fields: frozenset[str],
    *,
    include_diagnostics: bool = True,
) -> tuple[str, ...]:
    optional_fields = (
        TSHARK_OPTIONAL_FRAME_FIELD_NAMES
        if include_diagnostics
        else TSHARK_LIVE_OPTIONAL_FRAME_FIELD_NAMES
    )
    return TSHARK_FRAME_FIELD_NAMES + tuple(
        field
        for field in optional_fields
        if field in available_fields
    )


def build_survey_command(iface: str) -> list[str]:
    return ["iw", "dev", iface, "survey", "dump"]


def run_checked_command(command: list[str]) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise LiveCommandError(command=command, stderr=str(exc)) from exc
    if result.returncode != 0:
        raise LiveCommandError(
            command=command,
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
        )


def configure_monitor_interface(
    iface: str,
    channel: str,
    *,
    frequency_mhz: Optional[int] = None,
    band: Optional[str] = None,
    channel_definition: Optional[ChannelDefinition] = None,
    command_runner: Callable[[list[str]], None] = run_checked_command,
) -> None:
    for command in build_monitor_setup_commands(
        iface,
        channel,
        frequency_mhz=frequency_mhz,
        band=band,
        channel_definition=channel_definition,
    ):
        command_runner(command)


def start_tshark_process(
    iface: str,
    *,
    field_names: Optional[tuple[str, ...]] = None,
    raw_capture_path: Optional[Path] = None,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> subprocess.Popen[str]:
    if field_names is None:
        field_names = select_tshark_frame_fields(
            available_tshark_fields(),
            include_diagnostics=raw_capture_path is not None,
        )
    command = build_tshark_command(
        iface,
        field_names=field_names,
        raw_capture_path=raw_capture_path,
    )
    try:
        return popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise LiveCommandError(command=command, stderr=str(exc)) from exc


def read_survey_samples(iface: str) -> list[SurveySample]:
    command = build_survey_command(iface)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise LiveCommandError(
            command=command,
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
        )
    return parse_survey_dump(result.stdout)


def read_radio_capabilities(iface: str) -> RadioCapabilities:
    interface_command = ["iw", "dev", iface, "info"]
    result = subprocess.run(
        interface_command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise LiveCommandError(
            interface_command,
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
        )
    match = re.search(r"^\s*wiphy\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if match is None:
        raise LiveCommandError(interface_command, stderr="iw did not report a wiphy")
    phy_command = ["iw", "phy", f"phy{match.group(1)}", "info"]
    phy_result = subprocess.run(
        phy_command,
        capture_output=True,
        text=True,
        check=False,
    )
    if phy_result.returncode != 0:
        raise LiveCommandError(
            phy_command,
            returncode=phy_result.returncode,
            stderr=(phy_result.stderr or "").strip(),
        )
    return parse_iw_phy_capabilities(phy_result.stdout)


def read_actual_channel_definition(iface: str) -> ChannelDefinition:
    command = ["iw", "dev", iface, "info"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise LiveCommandError(
            command,
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
        )
    definition = parse_iw_interface_channel(result.stdout)
    if definition is None:
        raise LiveCommandError(command, stderr="iw did not report a channel definition")
    return definition


def explicit_channel_definition(
    *,
    primary_frequency_mhz: int,
    width: ChannelWidth,
    center_frequency1_mhz: Optional[int] = None,
    center_frequency2_mhz: Optional[int] = None,
) -> ChannelDefinition:
    if width is ChannelWidth.MHZ20:
        center_frequency1_mhz = primary_frequency_mhz
    elif center_frequency1_mhz is None:
        raise ValueError(
            f"{width.value} MHz requires --center-frequency1-mhz; "
            "the center cannot be derived from the primary alone"
        )
    if width is ChannelWidth.MHZ80P80 and center_frequency2_mhz is None:
        raise ValueError("80+80 MHz requires --center-frequency2-mhz")
    if width is not ChannelWidth.MHZ80P80 and center_frequency2_mhz is not None:
        raise ValueError("--center-frequency2-mhz is valid only with 80+80 MHz")
    definition = ChannelDefinition(
        primary_frequency_mhz,
        width,
        center_frequency1_mhz,
        center_frequency2_mhz,
        primary_channel=frequency_to_channel(primary_frequency_mhz),
        phy="override",
    )
    if primary_frequency_mhz not in definition.active_20mhz_centers:
        raise ValueError(
            "the explicit center frequency does not contain the selected "
            "primary/control channel"
        )
    return definition


def default_channel_definition(
    *,
    primary_frequency_mhz: int,
    band: str,
) -> ChannelDefinition:
    """Return the fixed WLAN Pi capture width for the selected band."""
    primary_channel = frequency_to_channel(primary_frequency_mhz)
    if band == "2.4":
        return ChannelDefinition(
            primary_frequency_mhz,
            ChannelWidth.MHZ20,
            primary_frequency_mhz,
            primary_channel=primary_channel,
            phy="band-default",
        )

    center_channels = (
        DEFAULT_5_GHZ_80MHZ_CENTER_CHANNELS
        if band == "5"
        else DEFAULT_6_GHZ_80MHZ_CENTER_CHANNELS
        if band == "6"
        else ()
    )
    for center_channel in center_channels:
        center_frequency = channel_to_frequency(center_channel, band)
        if center_frequency is None:
            continue
        definition = ChannelDefinition(
            primary_frequency_mhz,
            ChannelWidth.MHZ80,
            center_frequency,
            primary_channel=primary_channel,
            phy="band-default",
        )
        if primary_frequency_mhz in definition.active_20mhz_centers:
            return definition

    # Edge or nonstandard channels that cannot form a legal 80 MHz block use
    # the only definition that can be derived safely from the primary alone.
    return ChannelDefinition(
        primary_frequency_mhz,
        ChannelWidth.MHZ20,
        primary_frequency_mhz,
        primary_channel=primary_channel,
        phy="band-default-fallback",
        reason="selected primary is not inside a standard 80 MHz block",
    )


def update_fixed_capture_coverage(
    coverage: CoverageDecision,
    *,
    bssid: str,
    advertised: ChannelDefinition,
    actual: ChannelDefinition,
) -> CoverageDecision:
    """Record whether one target-primary BSSID fits the fixed capture width."""
    covered = set(coverage.covered_bssids)
    partial = set(coverage.partial_bssids)
    inferred_ht20 = (
        advertised.phy == "ht20-capture-inference"
        and actual.width is ChannelWidth.MHZ20
        and advertised.primary_frequency_mhz == actual.primary_frequency_mhz
    )
    if inferred_ht20 or (
        advertised.complete
        and not advertised.ambiguous
        and actual.contains(advertised)
    ):
        covered.add(bssid)
        partial.discard(bssid)
        status = (
            "complete"
            if coverage.status == "partial" and not partial
            else coverage.status
        )
        warning = None if status == "complete" else coverage.warning
        return CoverageDecision(
            coverage.requested,
            tuple(sorted(covered)),
            tuple(sorted(partial)),
            status,
            warning,
        )

    partial.add(bssid)
    covered.discard(bssid)
    reason = advertised.reason or (
        f"advertised {advertised.width.value} MHz is not fully covered by "
        f"actual {actual.width.value} MHz capture"
    )
    status = "fallback" if coverage.status == "fallback" else "partial"
    return CoverageDecision(
        coverage.requested,
        tuple(sorted(covered)),
        tuple(sorted(partial)),
        status,
        reason,
    )


def _read_radio_capabilities_safely(iface: str) -> RadioCapabilities:
    try:
        return read_radio_capabilities(iface)
    except (LiveCommandError, OSError, ValueError):
        return RadioCapabilities.ht20_only()


def _read_actual_channel_safely(
    iface: str,
    requested: ChannelDefinition,
) -> tuple[ChannelDefinition, bool]:
    try:
        return read_actual_channel_definition(iface), True
    except (LiveCommandError, OSError, ValueError):
        return requested, False


def terminate_tshark_process(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float = 3.0,
) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def run_live(
    *,
    iface: str = "wlan0",
    channel: str = "36",
    frequency_mhz: Optional[int] = None,
    band: Optional[str] = None,
    channel_width: Optional[ChannelWidth] = None,
    center_frequency1_mhz: Optional[int] = None,
    center_frequency2_mhz: Optional[int] = None,
    raw_capture_path: Optional[Path] = None,
    interval_seconds: float = 1.0,
    local_cu: bool = False,
    survey_debug: bool = False,
    stats_csv: Optional[Path] = None,
    beacons_jsonl: Optional[Path] = None,
    lcd_frame: Optional[Path] = None,
    logging_only: bool = False,
    logging_control_path: Optional[Path] = None,
    logging_status_path: Optional[Path] = None,
    initial_logging_enabled: Optional[bool] = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_check_interval_seconds: float = DEFAULT_DISK_CHECK_INTERVAL_SECONDS,
    rotation_interval_seconds: float = DEFAULT_ROTATION_INTERVAL_SECONDS,
    _dashboard: Optional[LiveDashboard] = None,
) -> int:
    if logging_only and stats_csv is None and beacons_jsonl is None:
        raise ValueError("logging-only mode requires at least one log format")
    local_cu = local_cu or survey_debug
    resolved_frequency_mhz = resolve_survey_target_frequency_mhz(
        channel,
        frequency_mhz=frequency_mhz,
        band=band,
    )
    resolved_band = band or frequency_to_band(resolved_frequency_mhz)
    if (
        _dashboard is None
        and not logging_only
        and lcd_frame is None
        and should_use_curses()
    ):
        curses_dashboard = CursesDashboard(
            include_local_cu=local_cu,
            band=resolved_band,
            channel=channel,
            frequency_mhz=resolved_frequency_mhz,
        )
        return curses_dashboard.run(
            lambda: run_live(
                iface=iface,
                channel=channel,
                frequency_mhz=frequency_mhz,
                band=band,
                channel_width=channel_width,
                center_frequency1_mhz=center_frequency1_mhz,
                center_frequency2_mhz=center_frequency2_mhz,
                raw_capture_path=raw_capture_path,
                interval_seconds=interval_seconds,
                local_cu=local_cu,
                survey_debug=survey_debug,
                stats_csv=stats_csv,
                beacons_jsonl=beacons_jsonl,
                lcd_frame=lcd_frame,
                logging_only=logging_only,
                logging_control_path=logging_control_path,
                logging_status_path=logging_status_path,
                initial_logging_enabled=initial_logging_enabled,
                min_free_bytes=min_free_bytes,
                disk_check_interval_seconds=disk_check_interval_seconds,
                rotation_interval_seconds=rotation_interval_seconds,
                _dashboard=curses_dashboard,
            )
        )
    target_frequency_mhz = resolved_frequency_mhz if local_cu else None
    if resolved_frequency_mhz is None:
        raise ValueError(
            "the selected primary channel is ambiguous; provide --band or "
            "--frequency-mhz"
        )
    if resolved_band is None:
        raise ValueError(
            f"frequency {resolved_frequency_mhz} MHz is outside the supported "
            "2.4, 5, and 6 GHz channel ranges"
        )
    auto_channel_width = channel_width is None
    requested_definition = (
        default_channel_definition(
            primary_frequency_mhz=resolved_frequency_mhz,
            band=resolved_band,
        )
        if auto_channel_width
        else explicit_channel_definition(
            primary_frequency_mhz=resolved_frequency_mhz,
            width=channel_width,
            center_frequency1_mhz=center_frequency1_mhz,
            center_frequency2_mhz=center_frequency2_mhz,
        )
    )
    capabilities = _read_radio_capabilities_safely(iface)
    supported, unsupported_reason = capabilities.supports(requested_definition)
    initial_definition = requested_definition
    fallback_reason: Optional[str] = None
    if (
        auto_channel_width
        and resolved_band in {"5", "6"}
        and requested_definition.width is ChannelWidth.MHZ20
    ):
        fallback_reason = requested_definition.reason
        print(
            "Warning: the selected primary is not inside a standard 80 MHz "
            "block; using 20 MHz capture.",
            file=sys.stderr,
            flush=True,
        )
    if not supported:
        if not auto_channel_width:
            raise ValueError(
                "explicit capture definition is unsupported: "
                f"{unsupported_reason}"
            )
        initial_definition = ChannelDefinition(
            resolved_frequency_mhz,
            ChannelWidth.MHZ20,
            resolved_frequency_mhz,
            primary_channel=frequency_to_channel(resolved_frequency_mhz),
            phy="fallback",
        )
        fallback_reason = unsupported_reason
        print(
            "Warning: requested capture definition is unsupported "
            f"({unsupported_reason}); falling back to HT20 partial coverage.",
            file=sys.stderr,
            flush=True,
        )
    try:
        configure_monitor_interface(
            iface,
            channel,
            frequency_mhz=frequency_mhz,
            band=band,
            channel_definition=initial_definition,
        )
    except LiveCommandError as exc:
        if not auto_channel_width or initial_definition.width is ChannelWidth.MHZ20:
            raise
        fallback_reason = str(exc)
        initial_definition = ChannelDefinition(
            resolved_frequency_mhz,
            ChannelWidth.MHZ20,
            resolved_frequency_mhz,
            primary_channel=frequency_to_channel(resolved_frequency_mhz),
            phy="tune-fallback",
        )
        print(
            "Warning: default 80 MHz capture could not be configured "
            f"({exc}); retrying once at 20 MHz.",
            file=sys.stderr,
            flush=True,
        )
        configure_monitor_interface(
            iface,
            channel,
            frequency_mhz=frequency_mhz,
            band=band,
            channel_definition=initial_definition,
        )
    actual_definition, actual_verified = _read_actual_channel_safely(
        iface,
        initial_definition,
    )
    if not actual_verified and capabilities.source_complete:
        print(
            "Warning: iw could not verify the actual capture width; "
            f"requested {initial_definition.label}.",
            file=sys.stderr,
            flush=True,
        )
    elif not actual_definition.same_tuning(initial_definition):
        fallback_reason = (
            f"iw reported {actual_definition.label} after requesting "
            f"{initial_definition.label}"
        )
        print(
            "Warning: actual capture definition differs from the request: "
            f"requested {initial_definition.label}; actual {actual_definition.label}.",
            file=sys.stderr,
            flush=True,
        )
    coverage_decision = CoverageDecision(
        requested_definition,
        (),
        (),
        (
            "fallback"
            if fallback_reason is not None
            else "unverified"
            if not actual_verified
            else "complete"
        ),
        fallback_reason,
    )
    analyzer = Analyzer(
        target_primary_frequency_mhz=resolved_frequency_mhz,
        channel_definition_max_age_seconds=CHANNEL_DEFINITION_MAX_AGE_SECONDS,
    )
    dashboard: LiveDashboard
    if _dashboard is not None:
        dashboard = _dashboard
    elif logging_only:
        dashboard = NullDashboard()
    elif lcd_frame is not None:
        dashboard = LcdDashboard(
            lcd_frame,
            band=resolved_band,
            channel=channel,
            frequency_mhz=resolved_frequency_mhz,
        )
    else:
        dashboard = TerminalDashboard(
            include_local_cu=local_cu,
            band=resolved_band,
            channel=channel,
            frequency_mhz=resolved_frequency_mhz,
        )
    _set_dashboard_capture_width(
        dashboard,
        actual_definition.width.value,
    )
    logging_service: Optional[LoggingService] = None
    if stats_csv is not None or beacons_jsonl is not None:
        first_log_path = stats_csv if stats_csv is not None else beacons_jsonl
        assert first_log_path is not None
        log_dir = first_log_path.parent
        logging_service = LoggingService(
            metadata=LogMetadata(
                interface=iface,
                channel=channel,
                frequency_mhz=resolved_frequency_mhz,
                band=resolved_band,
                capture_width_mode=(
                    "band-default" if auto_channel_width else "explicit"
                ),
            ).with_channel_definition(
                requested=requested_definition,
                actual=actual_definition,
                verified=actual_verified,
                coverage_status=coverage_decision.status,
            ),
            log_dir=log_dir,
            write_stats_csv=stats_csv is not None,
            write_beacons_jsonl=beacons_jsonl is not None,
            initial_paths=(
                None
                if initial_logging_enabled is False
                else LiveLogPaths(stats_csv, beacons_jsonl)
            ),
            min_free_bytes=min_free_bytes,
            disk_check_interval_seconds=disk_check_interval_seconds,
            rotation_interval_seconds=rotation_interval_seconds,
            monotonic=time.monotonic,
        )
    tshark_available_fields = available_tshark_fields()
    tshark_fields = select_tshark_frame_fields(
        tshark_available_fields,
        include_diagnostics=raw_capture_path is not None,
    )
    if (
        auto_channel_width
        and tshark_available_fields
        and not any(
            field in tshark_fields
            for field in (
                "wlan.ds.current_channel",
                "wlan.ht.info.primarychannel",
                "wlan.ext_tag.he_operation.6ghz.primary_channel",
            )
        )
        and actual_definition.width is not ChannelWidth.MHZ20
    ):
        print(
            "Warning: installed TShark exposes no supported channel-operation "
            "fields; retry metrics cannot safely associate BSSIDs with the "
            "selected primary inside this bonded capture.",
            file=sys.stderr,
            flush=True,
        )
    if raw_capture_path is not None:
        raw_capture_path.parent.mkdir(parents=True, exist_ok=True)
    process = start_tshark_process(
        iface,
        field_names=tshark_fields,
        raw_capture_path=raw_capture_path,
    )
    selector = selectors.DefaultSelector()
    warmup_filter = LiveWarmupFilter()
    warned_truncated_beacons: set[str] = set()
    warned_partial_bssids: set[str] = set()
    capture_record_count = 0
    normalized_frame_count = 0
    malformed_capture_record_count = 0

    initial_logging_requested = (
        logging_service is not None
        if initial_logging_enabled is None
        else initial_logging_enabled and logging_service is not None
    )
    if logging_control_path is not None:
        requested = _read_logging_control(logging_control_path)
        if requested is not None:
            initial_logging_requested = requested

    previous_survey_samples = _read_survey_samples_safely(iface) if local_cu else None
    latest_local_cu_percent: Optional[float] = None
    next_survey_poll = time.monotonic() + interval_seconds
    next_logging_control_poll = time.monotonic()
    survey_warning_printed = False
    low_disk_latched = False

    try:
        if logging_service is not None and initial_logging_requested:
            if logging_service.start():
                status_rendered = _set_dashboard_logging_status(
                    dashboard,
                    active=True,
                    paths=logging_service.current_paths,
                )
                if not status_rendered:
                    _print_logging_started(logging_service)
        if process.stdout is None:
            raise LiveCommandError(build_tshark_command(iface), stderr="missing stdout")
        selector.register(process.stdout, selectors.EVENT_READ)

        while True:
            if _dashboard_requests_exit(dashboard):
                raise KeyboardInterrupt
            loop_now = time.monotonic()
            timeout_deadlines = [next_survey_poll]
            if logging_control_path is not None:
                timeout_deadlines.append(next_logging_control_poll)
            if logging_service is not None:
                maintenance_wait = logging_service.seconds_until_maintenance(
                    now=loop_now
                )
                if maintenance_wait is not None:
                    timeout_deadlines.append(loop_now + maintenance_wait)
            dashboard_poll_interval = getattr(
                dashboard,
                "poll_interval_seconds",
                None,
            )
            if (
                isinstance(dashboard_poll_interval, (int, float))
                and dashboard_poll_interval > 0
            ):
                timeout_deadlines.append(loop_now + dashboard_poll_interval)
            timeout = max(0.0, min(timeout_deadlines) - loop_now)
            events = selector.select(timeout)
            for key, _ in events:
                line = _read_tshark_line(key.fileobj)
                if line == "":
                    include_history = warmup_filter.remaining_cycles <= 0
                    completed_stats = analyzer.publish_capture_complete(
                        latest_local_cu_percent,
                        include_history=include_history,
                        capture_ended=True,
                    )
                    _publish_live_stats(
                        warmup_filter.filter(completed_stats),
                        stats_writer=(
                            logging_service.write_stats
                            if logging_service is not None
                            else _discard_stats
                        ),
                        dashboard=dashboard,
                        snapshot=analyzer.snapshot,
                    )
                    return _handle_tshark_exit(process)
                capture_record_count += 1
                frames = parse_tshark_capture_record(
                    line,
                    field_names=tshark_fields,
                    band=resolved_band,
                )
                if not frames:
                    malformed_capture_record_count += 1
                normalized_frame_count += len(frames)
                for parsed_frame in frames:
                    frame = parsed_frame
                    if (
                        frame.is_beacon
                        and frame.original_length is not None
                        and frame.captured_length is not None
                        and frame.original_length > frame.captured_length
                    ):
                        warning_key = frame.bssid or "unknown-bssid"
                        if warning_key not in warned_truncated_beacons:
                            print(
                                "Warning: beacon "
                                f"{warning_key} is {frame.original_length} bytes "
                                f"and was truncated to {frame.captured_length}; "
                                "information elements beyond the live 512-byte "
                                "limit may be unavailable. Use --raw-pcapng for "
                                "a full-length diagnostic capture.",
                                file=sys.stderr,
                                flush=True,
                            )
                            warned_truncated_beacons.add(warning_key)
                    if (
                        frame.is_beacon
                        and frame.channel_definition is None
                        and actual_definition.width is ChannelWidth.MHZ20
                    ):
                        # A beacon decoded while physically restricted to the
                        # selected 20 MHz control channel is safe to associate
                        # with that primary, but its advertised width remains
                        # explicitly unknown/incomplete.
                        frame = replace(
                            frame,
                            channel_definition=ChannelDefinition(
                                resolved_frequency_mhz,
                                ChannelWidth.MHZ20,
                                resolved_frequency_mhz,
                                primary_channel=frequency_to_channel(
                                    resolved_frequency_mhz
                                ),
                                phy="ht20-capture-inference",
                                complete=False,
                                ambiguous=True,
                                reason=(
                                    "operation fields unavailable; primary "
                                    "inferred from verified HT20 capture"
                                ),
                            ),
                        )
                    if frame.is_beacon and frame.channel_definition is not None:
                        definition_supported, definition_reason = (
                            capabilities.supports(frame.channel_definition)
                        )
                        if not definition_supported:
                            frame = replace(
                                frame,
                                channel_definition=replace(
                                    frame.channel_definition,
                                    supported=False,
                                    reason=definition_reason,
                                ),
                            )
                    beacon = frame.beacon_record()
                    if (
                        beacon is not None
                        and beacon.channel_definition is not None
                        and beacon.channel_definition.primary_frequency_mhz
                        == resolved_frequency_mhz
                    ):
                        updated_coverage = update_fixed_capture_coverage(
                            coverage_decision,
                            bssid=beacon.bssid,
                            advertised=beacon.channel_definition,
                            actual=actual_definition,
                        )
                        if (
                            beacon.bssid in updated_coverage.partial_bssids
                            and beacon.bssid not in warned_partial_bssids
                        ):
                            print(
                                "Warning: fixed capture has partial coverage "
                                f"for {beacon.bssid}: "
                                f"{updated_coverage.warning}.",
                                file=sys.stderr,
                                flush=True,
                            )
                            warned_partial_bssids.add(beacon.bssid)
                        if updated_coverage != coverage_decision:
                            coverage_decision = updated_coverage
                            if logging_service is not None:
                                logging_service.update_channel_definition(
                                    requested=coverage_decision.requested,
                                    actual=actual_definition,
                                    verified=actual_verified,
                                    coverage_status=coverage_decision.status,
                                )
                    if beacon is not None and logging_service is not None:
                        logging_service.write_beacon(beacon)
                    # Defer frame-level snapshots. The capture-watermark call
                    # below publishes only after this frame passes a second
                    # boundary plus beacon-delay grace, avoiding a rebuild for
                    # every busy-channel row.
                    analyzer.ingest(frame, publish_snapshot=False)
                    if analyzer.has_ready_stats:
                        include_history = warmup_filter.remaining_cycles <= 0
                        completed_stats = analyzer.publish_capture_complete(
                            latest_local_cu_percent,
                            include_history=include_history,
                        )
                        _publish_live_stats(
                            warmup_filter.filter(completed_stats),
                            stats_writer=(
                                logging_service.write_stats
                                if logging_service is not None
                                else _discard_stats
                            ),
                            dashboard=dashboard,
                            snapshot=analyzer.snapshot,
                        )

            if _dashboard_requests_exit(dashboard):
                raise KeyboardInterrupt

            current = time.monotonic()
            if (
                logging_control_path is not None
                and current >= next_logging_control_poll
            ):
                requested = _read_logging_control(logging_control_path)
                if requested is False:
                    low_disk_latched = False
                if logging_service is not None and requested is not None:
                    if requested and not logging_service.active and not low_disk_latched:
                        if logging_service.start():
                            status_rendered = _set_dashboard_logging_status(
                                dashboard,
                                active=True,
                                paths=logging_service.current_paths,
                            )
                            if not status_rendered:
                                _print_logging_started(logging_service)
                    elif not requested and logging_service.active:
                        logging_service.stop()
                        status_rendered = _set_dashboard_logging_status(
                            dashboard,
                            active=False,
                            paths=logging_service.current_paths,
                            message="Logging stopped",
                        )
                        if not status_rendered:
                            print("Logging stopped.", file=sys.stderr, flush=True)
                        if logging_only:
                            return 0
                next_logging_control_poll = _advance_interval_deadline(
                    next_logging_control_poll,
                    LOGGING_CONTROL_POLL_SECONDS,
                    current,
                )

            if logging_service is not None:
                logging_event = logging_service.maintain(now=current)
                if logging_event is LoggingEvent.LOW_DISK_STOP:
                    low_disk_latched = True
                    status_rendered = _set_dashboard_logging_status(
                        dashboard,
                        active=False,
                        paths=logging_service.current_paths,
                        message="Logging stopped: low disk space",
                    )
                    if logging_status_path is not None:
                        _write_logging_status(
                            logging_status_path,
                            reason="disk_space_nearly_full",
                        )
                    if not status_rendered:
                        print(
                            "Logging stopped: free disk space is below the safety "
                            "threshold.",
                            file=sys.stderr,
                            flush=True,
                        )
                    if logging_only:
                        return 0
                elif logging_event is LoggingEvent.ROTATED:
                    status_rendered = _set_dashboard_logging_status(
                        dashboard,
                        active=True,
                        paths=logging_service.current_paths,
                    )
                    if not status_rendered:
                        _print_logging_started(
                            logging_service,
                            prefix="Log rollover",
                        )

            if current >= next_survey_poll:
                if local_cu:
                    current_survey_samples = _read_survey_samples_safely(iface)
                    if current_survey_samples is None:
                        if not survey_warning_printed:
                            print(
                                "Warning: survey counters unavailable; "
                                "local CU marked unavailable",
                                file=sys.stderr,
                                flush=True,
                            )
                            survey_warning_printed = True
                        latest_local_cu_percent = None
                    else:
                        if previous_survey_samples is not None:
                            survey_cu_result = compute_local_cu_result_from_samples(
                                previous_survey_samples,
                                current_survey_samples,
                                target_frequency_mhz=target_frequency_mhz,
                            )
                            latest_local_cu_percent = (
                                survey_cu_result.local_cu_percent
                            )
                            if survey_debug:
                                print(
                                    _format_survey_debug_line(survey_cu_result),
                                    file=sys.stderr,
                                    flush=True,
                                )
                            elif (
                                latest_local_cu_percent is None
                                and not survey_warning_printed
                            ):
                                print(
                                    _format_survey_unavailable_warning(
                                        survey_cu_result
                                    ),
                                    file=sys.stderr,
                                    flush=True,
                                )
                                survey_warning_printed = True
                        previous_survey_samples = current_survey_samples

                next_survey_poll = _advance_interval_deadline(
                    next_survey_poll,
                    interval_seconds,
                    current,
                )
    except KeyboardInterrupt:
        print("\nStopping live capture...", file=sys.stderr, flush=True)
        include_history = warmup_filter.remaining_cycles <= 0
        pending_stats = analyzer.flush(include_history=include_history)
        _publish_live_stats(
            warmup_filter.filter(pending_stats),
            stats_writer=(
                logging_service.write_stats
                if logging_service is not None
                else _discard_stats
            ),
            dashboard=dashboard,
            snapshot=analyzer.snapshot,
        )
        return 0
    finally:
        if logging_service is not None:
            logging_service.stop()
        selector.close()
        terminate_tshark_process(process)
        if raw_capture_path is not None:
            _write_raw_capture_metadata(
                raw_capture_path,
                interface=iface,
                selected_channel=channel,
                requested=coverage_decision.requested,
                actual=actual_definition,
                actual_verified=actual_verified,
                coverage=coverage_decision,
                tshark_fields=tshark_fields,
                capture_record_count=capture_record_count,
                normalized_frame_count=normalized_frame_count,
                malformed_capture_record_count=malformed_capture_record_count,
                tshark_stderr=_read_process_stderr(process),
            )


def _read_survey_samples_safely(iface: str) -> Optional[list[SurveySample]]:
    try:
        return read_survey_samples(iface)
    except (LiveCommandError, OSError, ValueError, OverflowError):
        return None


def _read_tshark_line(fileobj: object) -> str:
    return fileobj.readline() if hasattr(fileobj, "readline") else ""


def _read_process_stderr(process: subprocess.Popen[str]) -> str:
    try:
        return process.stderr.read().strip() if process.stderr is not None else ""
    except (OSError, ValueError):
        return ""


def _write_raw_capture_metadata(
    raw_capture_path: Path,
    *,
    interface: str,
    selected_channel: str,
    requested: ChannelDefinition,
    actual: ChannelDefinition,
    actual_verified: bool,
    coverage: CoverageDecision,
    tshark_fields: tuple[str, ...],
    capture_record_count: int,
    normalized_frame_count: int,
    malformed_capture_record_count: int,
    tshark_stderr: str,
) -> None:
    """Write an auditable sidecar for the same-process full PCAPNG stream."""
    drop_match = re.search(r"(\d+)\s+packets?\s+dropped", tshark_stderr, re.I)
    payload = {
        "raw_capture": str(raw_capture_path),
        "interface": interface,
        "selected_primary_channel": selected_channel,
        "requested_channel_definition": asdict(requested),
        "actual_channel_definition": asdict(actual),
        "actual_channel_definition_verified": actual_verified,
        "coverage_status": coverage.status,
        "covered_bssids": coverage.covered_bssids,
        "partial_bssids": coverage.partial_bssids,
        "coverage_warning": coverage.warning,
        "snapshot_length": 0,
        "snapshot_length_note": "full packet capture; no application truncation",
        "tshark_fields": tshark_fields,
        "capture_record_count": capture_record_count,
        "normalized_mpdu_count": normalized_frame_count,
        "malformed_or_non_wlan_record_count": malformed_capture_record_count,
        "reported_drop_count": (
            int(drop_match.group(1)) if drop_match is not None else None
        ),
        "drop_count_note": (
            "TShark did not report a drop count; inspect the PCAPNG interface "
            "statistics block and adapter/driver counters"
            if drop_match is None
            else "parsed from TShark capture status"
        ),
        "tshark_stderr": tshark_stderr,
    }
    sidecar = raw_capture_path.with_suffix(raw_capture_path.suffix + ".json")
    temporary = sidecar.with_name(f".{sidecar.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(sidecar)
    except OSError as exc:
        print(
            f"Warning: could not write raw-capture metadata {sidecar}: {exc}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _handle_tshark_exit(process: subprocess.Popen[str]) -> int:
    returncode = process.poll()
    if returncode in (None, 0):
        return 0

    stderr = ""
    if process.stderr is not None:
        stderr = process.stderr.read().strip()
    print(
        f"tshark exited with status {returncode}",
        file=sys.stderr,
        flush=True,
    )
    if stderr:
        print(stderr, file=sys.stderr, flush=True)
    return returncode


def _publish_live_stats(
    stats_rows: list[SecondStats],
    *,
    stats_writer: Callable[[SecondStats], None],
    dashboard: LiveDashboard,
    snapshot: MetricsSnapshot,
) -> None:
    if not stats_rows:
        return
    for stats in stats_rows:
        stats_writer(stats)
    dashboard.refresh(snapshot)


def _dashboard_requests_exit(dashboard: LiveDashboard) -> bool:
    poll_input = getattr(dashboard, "poll_input", None)
    return bool(poll_input()) if callable(poll_input) else False


def _set_dashboard_capture_width(
    dashboard: LiveDashboard,
    width: str,
) -> None:
    setter = getattr(dashboard, "set_capture_width", None)
    if callable(setter):
        setter(width)


def _set_dashboard_logging_status(
    dashboard: LiveDashboard,
    *,
    active: bool,
    paths: Optional[LiveLogPaths],
    message: Optional[str] = None,
) -> bool:
    setter = getattr(dashboard, "set_logging_status", None)
    if not callable(setter):
        return False
    path_values = None
    if paths is not None:
        path_values = tuple(
            path
            for path in (paths.stats_csv, paths.beacons_jsonl)
            if path is not None
        )
    setter(active=active, paths=path_values, message=message)
    return True


def _advance_interval_deadline(
    previous_deadline: float,
    interval_seconds: float,
    current: float,
) -> float:
    next_deadline = previous_deadline
    while next_deadline <= current:
        next_deadline += interval_seconds
    return next_deadline


def _read_logging_control(path: Path) -> Optional[bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload["logging_enabled"]
    except (KeyError, OSError, TypeError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, bool) else None


def _write_logging_status(path: Path, *, reason: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"reason": reason}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        # Notification IPC is best-effort and must never stop capture.
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _print_logging_started(
    logging_service: LoggingService,
    *,
    prefix: str = "Logging started",
) -> None:
    paths = logging_service.current_paths
    if paths is None:
        return
    if paths.stats_csv is not None:
        print(f"{prefix} - Stats CSV log: {paths.stats_csv}", file=sys.stderr, flush=True)
    if paths.beacons_jsonl is not None:
        print(
            f"{prefix} - Beacon JSONL log: {paths.beacons_jsonl}",
            file=sys.stderr,
            flush=True,
        )


def _discard_stats(stats: SecondStats) -> None:
    return None


def _format_optional_percent(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:.2f}%"


def _format_optional_int(value: Optional[int]) -> str:
    return "--" if value is None else str(value)


def _format_survey_debug_line(result: SurveyCuResult) -> str:
    frequency = (
        "--" if result.frequency_mhz is None else f"{result.frequency_mhz}MHz"
    )
    return (
        "survey_debug "
        f"freq={frequency} "
        f"active_delta_ms={_format_optional_int(result.active_delta_ms)} "
        f"busy_delta_ms={_format_optional_int(result.busy_delta_ms)} "
        f"local_survey_cu={_format_optional_percent(result.local_cu_percent)} "
        f"reason={result.reason}"
    )


def _format_survey_unavailable_warning(result: SurveyCuResult) -> str:
    return (
        f"Warning: local survey CU unavailable: {result.reason}. "
        "The adapter/driver may not expose usable active/busy counters for "
        "the tuned channel; use --survey-debug for details."
    )
