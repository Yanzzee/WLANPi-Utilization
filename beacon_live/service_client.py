"""Local Utilization controls and renderer attachment; no capture or analysis."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

from beacon_live.ipc import DEFAULT_SOCKET, PROTOCOL_VERSION, encode, from_wire, receive_blocking, to_wire
from beacon_live.runtime import ACTIVE_PHASES, LiveSessionConfig, RuntimePublication, SessionPhase


class ServiceError(RuntimeError):
    pass


class ServiceClient:
    def __init__(self, socket_path: Path = DEFAULT_SOCKET, *, timeout=5.0):
        self.socket_path, self.timeout = socket_path, timeout

    def _exchange(self, op, **values) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(self.socket_path))
                sock.sendall(encode({"version": PROTOCOL_VERSION, "op": op, **to_wire(values)}))
                response = receive_blocking(sock)
            except (OSError, EOFError, ValueError) as error:
                raise ServiceError(f"Utilization service unavailable at {self.socket_path}: {error}") from error
        if response.get("version") != PROTOCOL_VERSION:
            raise ServiceError("Unsupported Utilization service protocol")
        if response.get("ok") is not True:
            raise ServiceError(response.get("error", "Utilization service request failed"))
        return response

    def request(self, op, **values) -> RuntimePublication:
        response = self._exchange(op, **values)
        try:
            return from_wire(RuntimePublication, response.get("publication"))
        except (ValueError, TypeError) as error:
            raise ServiceError(f"Invalid Utilization publication: {error}") from error

    def read(self):
        return self.request("read")

    def files(self):
        return self._exchange("files")["files"]

    def ensure(self, config):
        return self.request("ensure", config=config)

    def stop(self, session_id):
        return self.request("stop", session_id=session_id)

    def set_logging(self, session_id, enabled):
        return self.request("logging", session_id=session_id, enabled=enabled)


def render_publication(dashboard, publication):
    for name, value in (
        ("set_collecting", publication.collecting),
        ("set_capture_width", publication.capture_width),
        ("set_notice", publication.notice),
    ):
        setter = getattr(dashboard, name, None)
        if callable(setter) and value is not None:
            setter(value)
    logging = getattr(dashboard, "set_logging_status", None)
    if callable(logging):
        logging(active=publication.logging.active, paths=publication.logging.paths,
                message=publication.logging.message)
    dashboard.refresh(publication.snapshot)


def follow(
    client, dashboard, *, initial=None, logging_control_path=None,
    logging_status_path=None, wait=time.sleep, clock=time.monotonic,
):
    """Detach on renderer exit; only explicit controls alter the shared owner."""
    publication = initial or client.read()
    if publication.session_id is None:
        raise ServiceError("No Utilization session; start one with --managed first")
    session_id = publication.session_id
    last_revision = -1
    next_read = clock() + 1.0
    last_logging_control = None
    if logging_control_path is not None:
        try:
            last_logging_control = json.loads(logging_control_path.read_text())["logging_enabled"]
        except (KeyError, ValueError, OSError, TypeError):
            pass
    try:
        while True:
            if publication.session_id != session_id:
                raise ServiceError("Utilization session changed; attach again to the new session")
            if publication.revision != last_revision:
                render_publication(dashboard, publication)
                last_revision = publication.revision
                if logging_status_path is not None:
                    from beacon_live.lcd_dashboard import _atomic_write
                    payload = {"logging_enabled": publication.logging.active, "reason": (
                        "disk_space_nearly_full"
                        if publication.logging.message == "Logging stopped: low disk space" else None
                    )}
                    _atomic_write(logging_status_path, json.dumps(payload).encode())
            if publication.phase not in ACTIVE_PHASES:
                if publication.phase is SessionPhase.FAILED:
                    raise ServiceError(publication.error or "Utilization capture failed")
                return publication.exit_code or (1 if publication.phase is SessionPhase.FAILED else 0)
            poll_input = getattr(dashboard, "poll_input", None)
            if callable(poll_input) and poll_input():
                return 0
            if logging_control_path is not None:
                try:
                    enabled = json.loads(logging_control_path.read_text())["logging_enabled"]
                except (KeyError, ValueError, OSError, TypeError):
                    enabled = None
                if type(enabled) is bool and enabled != last_logging_control:
                    client.set_logging(session_id, enabled)
                    last_logging_control = enabled
            wait(0.1)
            now = clock()
            if now >= next_read:
                publication = client.read()
                next_read = now + 1.0
    except KeyboardInterrupt:
        return 0


def managed_live(options, *, socket_path=DEFAULT_SOCKET, attach_only=False, start_only=False):
    from beacon_live.dashboard import TerminalDashboard
    from beacon_live.lcd_dashboard import LcdDashboard
    from beacon_live.live import NullDashboard, frequency_to_band, resolve_survey_target_frequency_mhz
    from beacon_live.tui import CursesDashboard, should_use_curses

    options = dict(options)
    lcd_frame = options.pop("lcd_frame", None)
    logging_only = options.pop("logging_only", False)
    options.pop("logging_control_path", None)
    status = options.pop("logging_status_path", None)
    client = ServiceClient(socket_path)
    publication = client.read() if attach_only else client.ensure(LiveSessionConfig(**options))
    # Joining a display-only consumer never disables logging owned elsewhere.
    if not attach_only and options.get("initial_logging_enabled"):
        client.set_logging(publication.session_id, True)
    if start_only:
        print(f"Utilization session: {publication.session_id} ({publication.phase.value})")
        return 0
    if publication.config is None:
        raise ServiceError("No Utilization session to display")
    config = publication.config
    frequency = resolve_survey_target_frequency_mhz(
        config.channel, frequency_mhz=config.frequency_mhz, band=config.band,
    )
    kwargs = dict(band=config.band or frequency_to_band(frequency), channel=config.channel,
                  frequency_mhz=frequency)
    # Managed FPMS controls call the service explicitly. Its legacy shared
    # control file must not let another renderer's startup disable logging.
    follow_kwargs = dict(initial=publication, logging_status_path=status)
    if lcd_frame is not None:
        return follow(client, LcdDashboard(lcd_frame, **kwargs), **follow_kwargs)
    if logging_only:
        return follow(client, NullDashboard(), **follow_kwargs)
    if should_use_curses():
        dashboard = CursesDashboard(include_local_cu=config.local_cu, **kwargs)
        return dashboard.run(lambda: follow(client, dashboard, **follow_kwargs))
    return follow(client, TerminalDashboard(include_local_cu=config.local_cu, **kwargs), **follow_kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wlanpi-utilization session", description="Control the shared Utilization session.")
    parser.add_argument("operation", choices=("status", "stop", "logging"))
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--session-id", help="Required for mutating controls; obtain it from status.")
    parser.add_argument("--enabled", choices=("yes", "no"))
    parser.add_argument("--summary", action="store_true", help="Print only session and logging state.")
    args = parser.parse_args(argv)
    if args.operation != "status" and not args.session_id:
        parser.error("--session-id is required for stop/logging")
    if args.operation == "logging" and args.enabled is None:
        parser.error("logging requires --enabled yes|no")
    try:
        client = ServiceClient(args.socket)
        if args.operation == "status":
            publication = client.read()
        elif args.operation == "stop":
            publication = client.stop(args.session_id)
        else:
            publication = client.set_logging(args.session_id, args.enabled == "yes")
        value = to_wire(publication)
        if args.summary:
            value = {key: value[key] for key in ("session_id", "phase", "logging", "error", "exit_code")}
        print(json.dumps(value, indent=2))
        return 0
    except ServiceError as error:
        print(str(error), file=sys.stderr)
        return 1
