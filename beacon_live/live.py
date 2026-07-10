"""Minimal live WLAN beacon capture loop."""

from __future__ import annotations

import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from typing import Callable, Optional

from beacon_live.aggregator import Aggregator
from beacon_live.models import SecondStats
from beacon_live.models import SurveySample
from beacon_live.parser import parse_tshark_row
from beacon_live.survey import compute_local_cu_percent_from_samples
from beacon_live.survey import parse_survey_dump

TSHARK_BEACON_FIELDS = [
    "frame.time_epoch",
    "wlan.ssid",
    "wlan.bssid",
    "wlan.qbss.cu",
    "wlan.qbss.scount",
    "wlan.qbss.adc",
]


@dataclass(frozen=True)
class LiveCommandError(Exception):
    command: list[str]
    returncode: Optional[int] = None
    stderr: str = ""

    @property
    def command_text(self) -> str:
        return " ".join(self.command)


def build_monitor_setup_commands(iface: str, channel: str) -> list[list[str]]:
    return [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
        ["iw", "dev", iface, "set", "channel", channel, "HT20"],
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
    command_runner: Callable[[list[str]], None] = run_checked_command,
) -> None:
    for command in build_monitor_setup_commands(iface, channel):
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
    interval_seconds: float = 1.0,
) -> int:
    configure_monitor_interface(iface, channel)
    process = start_tshark_process(iface)
    selector = selectors.DefaultSelector()
    aggregator = Aggregator()
    printed_seconds: set[int] = set()
    previous_survey_samples = _read_survey_samples_safely(iface)
    latest_local_cu_percent: Optional[float] = None
    next_survey_poll = time.monotonic() + interval_seconds
    survey_warning_printed = False

    print(_format_live_header(), flush=True)

    try:
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
                    aggregator.add_pending(record)

            if time.monotonic() >= next_survey_poll:
                current_survey_samples = _read_survey_samples_safely(iface)
                if current_survey_samples is None:
                    if not survey_warning_printed:
                        print(
                            "Warning: survey counters unavailable; local CU hidden",
                            file=sys.stderr,
                            flush=True,
                        )
                        survey_warning_printed = True
                    latest_local_cu_percent = None
                else:
                    if previous_survey_samples is not None:
                        latest_local_cu_percent = compute_local_cu_percent_from_samples(
                            previous_survey_samples,
                            current_survey_samples,
                        )
                    previous_survey_samples = current_survey_samples

                wall_second = int(time.time())
                _print_completed_live_stats(
                    aggregator,
                    wall_second,
                    latest_local_cu_percent,
                    printed_seconds,
                )
                next_survey_poll = _next_interval_deadline(
                    next_survey_poll,
                    interval_seconds,
                )
    except KeyboardInterrupt:
        print("\nStopping live capture...", file=sys.stderr, flush=True)
        for stats in aggregator.flush():
            if stats.second not in printed_seconds:
                stats = replace(stats, local_cu_percent=latest_local_cu_percent)
                print(_format_live_stats_line(stats), flush=True)
                printed_seconds.add(stats.second)
        return 0
    finally:
        selector.close()
        terminate_tshark_process(process)


def _read_survey_samples_safely(iface: str) -> Optional[list[SurveySample]]:
    try:
        return read_survey_samples(iface)
    except LiveCommandError:
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


def _print_completed_live_stats(
    aggregator: Aggregator,
    wall_second: int,
    local_cu_percent: Optional[float],
    printed_seconds: set[int],
) -> None:
    target_second = wall_second - 1
    completed_stats = aggregator.pop_completed_before(wall_second)
    completed_seconds = set()

    for stats in completed_stats:
        if stats.second in printed_seconds:
            continue
        completed_seconds.add(stats.second)
        stats = replace(stats, local_cu_percent=local_cu_percent)
        print(_format_live_stats_line(stats), flush=True)
        printed_seconds.add(stats.second)

    if target_second not in printed_seconds and target_second not in completed_seconds:
        print(
            _format_live_stats_line(
                _empty_second_stats(target_second, local_cu_percent)
            ),
            flush=True,
        )
        printed_seconds.add(target_second)


def _next_interval_deadline(previous_deadline: float, interval_seconds: float) -> float:
    next_deadline = previous_deadline + interval_seconds
    now = time.monotonic()
    while next_deadline <= now:
        next_deadline += interval_seconds
    return next_deadline


def _format_live_header() -> str:
    return (
        "time second unique_bssids qbss_station_sum max_qbss_cu "
        "top_qbss_cu local_cu"
    )


def _format_live_stats_line(stats: SecondStats) -> str:
    top_ssid = stats.top_qbss_cu_ssid or ""
    top_bssid = stats.top_qbss_cu_bssid or ""
    top_label = f"{top_ssid}/{top_bssid}".strip("/")
    return (
        f"time={_format_second_time(stats.second)} "
        f"second={stats.second} "
        f"unique_bssids={stats.unique_bssid_count} "
        f"qbss_station_sum={stats.qbss_station_count_sum} "
        f"max_qbss_cu={_format_optional_percent(stats.qbss_cu_max_percent)} "
        f"top_qbss_cu={top_label or '--'} "
        f"local_cu={_format_optional_percent(stats.local_cu_percent)}"
    )


def _empty_second_stats(
    second: int,
    local_cu_percent: Optional[float],
) -> SecondStats:
    return SecondStats(
        second=second,
        unique_bssid_count=0,
        qbss_station_count_sum=0,
        qbss_cu_min_percent=None,
        qbss_cu_mean_percent=None,
        qbss_cu_max_percent=None,
        top_qbss_cu_ssid=None,
        top_qbss_cu_bssid=None,
        top_qbss_cu_percent=None,
        local_cu_percent=local_cu_percent,
    )


def _format_second_time(second: int) -> str:
    return datetime.fromtimestamp(second, timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _format_optional_percent(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:.2f}%"
