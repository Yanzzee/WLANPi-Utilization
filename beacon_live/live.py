"""Minimal live WLAN frame capture loop."""

from __future__ import annotations

import json
import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from beacon_live.analyzer import Analyzer
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
from beacon_live.parser import parse_tshark_frame_row
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
from beacon_live.survey import SurveyCuResult
from beacon_live.survey import compute_local_cu_result_from_samples
from beacon_live.survey import parse_survey_dump
from beacon_live.tui import CursesDashboard
from beacon_live.tui import should_use_curses

TSHARK_CAPTURE_FIELDS = list(TSHARK_FRAME_FIELD_NAMES)
TSHARK_CAPTURE_PROTOCOLS = (
    "radiotap,wlan_radio,wlan,wlan_ext,wlan_aggregate"
)
TSHARK_CAPTURE_BUFFER_MIB = 16

SUPPORTED_BANDS = {"2.4", "5", "6"}
LIVE_WARMUP_CYCLES = 1
LOGGING_CONTROL_POLL_SECONDS = 0.5


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
        "-B",
        str(TSHARK_CAPTURE_BUFFER_MIB),
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
        "-Y",
        "wlan",
        "-T",
        "fields",
        "-E",
        "separator=\t",
        "-E",
        "occurrence=f",
    ]
    for field in TSHARK_CAPTURE_FIELDS:
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
    configure_monitor_interface(
        iface,
        channel,
        frequency_mhz=frequency_mhz,
        band=band,
    )
    analyzer = Analyzer()
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
        dashboard = TerminalDashboard(include_local_cu=local_cu)
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
    process = start_tshark_process(iface)
    selector = selectors.DefaultSelector()
    warmup_filter = LiveWarmupFilter()

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
                _print_logging_started(logging_service)
                _set_dashboard_logging_status(
                    dashboard,
                    active=True,
                    paths=logging_service.current_paths,
                )
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
                frame = parse_tshark_frame_row(line)
                if frame is not None:
                    beacon = frame.beacon_record()
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
                            _print_logging_started(logging_service)
                            _set_dashboard_logging_status(
                                dashboard,
                                active=True,
                                paths=logging_service.current_paths,
                            )
                    elif not requested and logging_service.active:
                        logging_service.stop()
                        _set_dashboard_logging_status(
                            dashboard,
                            active=False,
                            paths=logging_service.current_paths,
                            message="Logging stopped",
                        )
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
                    _set_dashboard_logging_status(
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
                    print(
                        "Logging stopped: free disk space is below the safety "
                        "threshold.",
                        file=sys.stderr,
                        flush=True,
                    )
                    if logging_only:
                        return 0
                elif logging_event is LoggingEvent.ROTATED:
                    _print_logging_started(logging_service, prefix="Log rollover")
                    _set_dashboard_logging_status(
                        dashboard,
                        active=True,
                        paths=logging_service.current_paths,
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


def _set_dashboard_logging_status(
    dashboard: LiveDashboard,
    *,
    active: bool,
    paths: Optional[LiveLogPaths],
    message: Optional[str] = None,
) -> None:
    setter = getattr(dashboard, "set_logging_status", None)
    if not callable(setter):
        return
    path_values = None
    if paths is not None:
        path_values = tuple(
            path
            for path in (paths.stats_csv, paths.beacons_jsonl)
            if path is not None
        )
    setter(active=active, paths=path_values, message=message)


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
