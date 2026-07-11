"""Minimal live WLAN beacon capture loop."""

from __future__ import annotations

import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from beacon_live.aggregator import Aggregator
from beacon_live.dashboard import TerminalDashboard
from beacon_live.log_writer import CaptureLogWriter
from beacon_live.log_writer import LogMetadata
from beacon_live.models import SecondStats
from beacon_live.models import SurveySample
from beacon_live.parser import parse_tshark_row
from beacon_live.parser import TSHARK_FIELD_NAMES
from beacon_live.survey import SurveyCuResult
from beacon_live.survey import compute_local_cu_result_from_samples
from beacon_live.survey import parse_survey_dump

TSHARK_BEACON_FIELDS = list(TSHARK_FIELD_NAMES)

SUPPORTED_BANDS = {"2.4", "5", "6"}
LIVE_WARMUP_CYCLES = 1


@dataclass(frozen=True)
class LiveCommandError(Exception):
    command: list[str]
    returncode: Optional[int] = None
    stderr: str = ""

    @property
    def command_text(self) -> str:
        return " ".join(self.command)


@dataclass
class LiveWarmupFilter:
    """Drop complete per-second stats from initial live refresh cycles."""

    remaining_cycles: int = LIVE_WARMUP_CYCLES

    def filter(self, stats_rows: list[SecondStats]) -> list[SecondStats]:
        if self.remaining_cycles > 0:
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
) -> list[list[str]]:
    return [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
        build_tune_command(
            iface,
            channel,
            frequency_mhz=frequency_mhz,
            band=band,
        ),
    ]


def build_tshark_command(iface: str) -> list[str]:
    command = [
        "tshark",
        "-l",
        "-i",
        iface,
        "-Y",
        "wlan.fc.type_subtype == 8",
        "-T",
        "fields",
        "-E",
        "separator=\t",
        "-E",
        "occurrence=f",
    ]
    for field in TSHARK_BEACON_FIELDS:
        command.extend(["-e", field])
    return command


def build_survey_command(iface: str) -> list[str]:
    return ["iw", "dev", iface, "survey", "dump"]


def run_checked_command(command: list[str]) -> None:
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


def configure_monitor_interface(
    iface: str,
    channel: str,
    *,
    frequency_mhz: Optional[int] = None,
    band: Optional[str] = None,
    command_runner: Callable[[list[str]], None] = run_checked_command,
) -> None:
    for command in build_monitor_setup_commands(
        iface,
        channel,
        frequency_mhz=frequency_mhz,
        band=band,
    ):
        command_runner(command)


def start_tshark_process(
    iface: str,
    *,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> subprocess.Popen[str]:
    command = build_tshark_command(iface)
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
    interval_seconds: float = 1.0,
    local_cu: bool = False,
    survey_debug: bool = False,
    stats_csv: Optional[Path] = None,
    beacons_jsonl: Optional[Path] = None,
) -> int:
    local_cu = local_cu or survey_debug
    resolved_frequency_mhz = resolve_survey_target_frequency_mhz(
        channel,
        frequency_mhz=frequency_mhz,
        band=band,
    )
    resolved_band = band or frequency_to_band(resolved_frequency_mhz)
    target_frequency_mhz = resolved_frequency_mhz if local_cu else None
    configure_monitor_interface(
        iface,
        channel,
        frequency_mhz=frequency_mhz,
        band=band,
    )
    process = start_tshark_process(iface)
    selector = selectors.DefaultSelector()
    aggregator = Aggregator()
    dashboard = TerminalDashboard(include_local_cu=local_cu)
    warmup_filter = LiveWarmupFilter()
    log_writer = CaptureLogWriter(
        metadata=LogMetadata(
            start_time=_local_now_iso(),
            interface=iface,
            channel=channel,
            frequency_mhz=resolved_frequency_mhz,
            band=resolved_band,
        ),
        stats_csv=stats_csv,
        beacons_jsonl=beacons_jsonl,
    )
    printed_seconds: set[int] = set()
    previous_survey_samples = _read_survey_samples_safely(iface) if local_cu else None
    latest_local_cu_percent: Optional[float] = None
    next_survey_poll = time.monotonic() + interval_seconds
    survey_warning_printed = False

    try:
        log_writer.open()
        if stats_csv is not None:
            print(f"Stats CSV log: {stats_csv}", file=sys.stderr, flush=True)
        if beacons_jsonl is not None:
            print(f"Beacon JSONL log: {beacons_jsonl}", file=sys.stderr, flush=True)
        if process.stdout is None:
            raise LiveCommandError(build_tshark_command(iface), stderr="missing stdout")
        selector.register(process.stdout, selectors.EVENT_READ)

        while True:
            timeout = max(0.0, next_survey_poll - time.monotonic())
            events = selector.select(timeout)
            for key, _ in events:
                line = _read_tshark_line(key.fileobj)
                if line == "":
                    return _handle_tshark_exit(process)
                record = parse_tshark_row(line)
                if record is not None:
                    log_writer.write_beacon(record)
                    aggregator.add_pending(record)

            if time.monotonic() >= next_survey_poll:
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

                wall_second = int(time.time())
                completed_stats = _completed_live_stats(
                    aggregator,
                    wall_second,
                    latest_local_cu_percent,
                    printed_seconds,
                )
                _publish_live_stats(
                    warmup_filter.filter(completed_stats),
                    stats_writer=log_writer.write_stats,
                    dashboard=dashboard,
                )
                next_survey_poll = _next_interval_deadline(
                    next_survey_poll,
                    interval_seconds,
                )
    except KeyboardInterrupt:
        print("\nStopping live capture...", file=sys.stderr, flush=True)
        pending_stats: list[SecondStats] = []
        for stats in aggregator.flush():
            if stats.second not in printed_seconds:
                stats = replace(stats, local_cu_percent=latest_local_cu_percent)
                pending_stats.append(stats)
                printed_seconds.add(stats.second)
        _publish_live_stats(
            warmup_filter.filter(pending_stats),
            stats_writer=log_writer.write_stats,
            dashboard=dashboard,
        )
        return 0
    finally:
        log_writer.close()
        selector.close()
        terminate_tshark_process(process)


def _read_survey_samples_safely(iface: str) -> Optional[list[SurveySample]]:
    try:
        return read_survey_samples(iface)
    except (LiveCommandError, OSError, ValueError, OverflowError):
        return None


def _read_tshark_line(fileobj: object) -> str:
    return fileobj.readline() if hasattr(fileobj, "readline") else ""


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


def _completed_live_stats(
    aggregator: Aggregator,
    wall_second: int,
    local_cu_percent: Optional[float],
    printed_seconds: set[int],
) -> list[SecondStats]:
    target_second = wall_second - 1
    completed_stats = aggregator.pop_completed_before(wall_second)
    completed_seconds = set()
    stats_to_publish: list[SecondStats] = []

    for stats in completed_stats:
        if stats.second in printed_seconds:
            continue
        completed_seconds.add(stats.second)
        stats = replace(stats, local_cu_percent=local_cu_percent)
        stats_to_publish.append(stats)
        printed_seconds.add(stats.second)

    if target_second not in printed_seconds and target_second not in completed_seconds:
        stats_to_publish.append(
            _empty_second_stats(target_second, local_cu_percent)
        )
        printed_seconds.add(target_second)

    return stats_to_publish


def _publish_live_stats(
    stats_rows: list[SecondStats],
    *,
    stats_writer: Callable[[SecondStats], None],
    dashboard: TerminalDashboard,
) -> None:
    if not stats_rows:
        return
    for stats in stats_rows:
        stats_writer(stats)
    dashboard.refresh(stats_rows)


def _next_interval_deadline(previous_deadline: float, interval_seconds: float) -> float:
    next_deadline = previous_deadline + interval_seconds
    now = time.monotonic()
    while next_deadline <= now:
        next_deadline += interval_seconds
    return next_deadline


def _empty_second_stats(
    second: int,
    local_cu_percent: Optional[float],
) -> SecondStats:
    return SecondStats(
        second=second,
        unique_bssid_count=0,
        qbss_station_count_sum=0,
        selected_qbss_cu_percent=None,
        selected_qbss_ssid=None,
        selected_qbss_bssid=None,
        selected_qbss_rssi_dbm=None,
        local_cu_percent=local_cu_percent,
    )


def _local_now_iso() -> str:
    return datetime.now().astimezone().isoformat()


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
