"""Utilization's local service and isolated capture owner process.

The broker has no radio/analyzer. The owner runs capture on its main thread
and polls nonblocking IPC; slow renderers cannot hold up decoder input.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from beacon_live.ipc import (
    DEFAULT_SOCKET, PROTOCOL_VERSION, FramedSocket, from_wire,
    receive_blocking, to_wire,
)
from beacon_live.models import MetricsSnapshot
from beacon_live.runtime import (
    ACTIVE_PHASES, LiveSessionConfig, ManagedRuntime, RuntimePublication, SessionPhase,
)

MAX_CLIENTS = 16
CLIENT_TIMEOUT = 5.0
OWNER_STOP_TIMEOUT = 95.0
SOURCE_FIELDS = (
    "iface", "netns", "capture_backend", "channel", "band", "frequency_mhz",
    "channel_width", "center_frequency1_mhz", "center_frequency2_mhz",
    "interval_seconds", "local_cu", "survey_debug", "raw_capture_path",
    "core_diagnostics_path",
)


def validate_config(config: LiveSessionConfig) -> None:
    from beacon_live.live import (
        SUPPORTED_BANDS, explicit_channel_definition, resolve_survey_target_frequency_mhz,
    )

    if config.capture_backend not in {"direct", "core"}:
        raise ValueError("Capture backend must be direct or core")
    if config.band is not None and config.band not in SUPPORTED_BANDS:
        raise ValueError("Unsupported band")
    if config.band is not None and config.frequency_mhz is not None:
        raise ValueError("Use frequency or band/channel, not both")
    if not config.iface or config.interval_seconds <= 0:
        raise ValueError("Interface and positive sampling interval are required")
    if config.min_free_bytes < 0 or min(
        config.disk_check_interval_seconds, config.rotation_interval_seconds
    ) <= 0:
        raise ValueError("Invalid logging limits")
    frequency = resolve_survey_target_frequency_mhz(
        config.channel, frequency_mhz=config.frequency_mhz, band=config.band,
    )
    if frequency is None:
        raise ValueError("Specify a band or unambiguous frequency")
    if config.channel_width is not None:
        explicit_channel_definition(
            primary_frequency_mhz=frequency, width=config.channel_width,
            center_frequency1_mhz=config.center_frequency1_mhz,
            center_frequency2_mhz=config.center_frequency2_mhz,
        )
    elif config.center_frequency1_mhz is not None or config.center_frequency2_mhz is not None:
        raise ValueError("Center frequencies require an explicit channel width")


class ServiceBroker:
    """Serialized controls and a latest publication, independent of connections."""

    def __init__(self, directory: Path, *, owner_command=None):
        self.directory = directory
        self.owner_command = owner_command or [sys.executable, "-m", "beacon_live.service"]
        self.publication = RuntimePublication(
            None, 0, SessionPhase.IDLE, None, MetricsSnapshot.empty(),
        )
        self.process = None
        self.owner = None
        self.control_path = None

    def _update(self, **changes):
        self.publication = replace(
            self.publication, revision=self.publication.revision + 1, **changes,
        )

    def dispatch(self, request: dict) -> dict:
        if type(request.get("version")) is not int or request.get("version") != PROTOCOL_VERSION:
            raise ValueError("Unsupported Utilization IPC protocol version")
        self.poll_owner()
        op = request.get("op")
        if op == "read":
            pass
        elif op in {"start", "ensure"}:
            config = from_wire(LiveSessionConfig, request.get("config"))
            config = replace(config, local_cu=config.local_cu or config.survey_debug)
            validate_config(config)
            if self.process is not None or self.publication.phase in ACTIVE_PHASES:
                compatible = (
                    op == "ensure" and self.publication.phase in {
                        SessionPhase.STARTING, SessionPhase.RUNNING,
                    } and all(getattr(config, key) == getattr(self.publication.config, key)
                              for key in SOURCE_FIELDS)
                )
                if not compatible:
                    raise ValueError("A Utilization session owns the radio; stop it before changing source")
            else:
                self._start(config)
        elif op in {"stop", "logging"}:
            if request.get("session_id") != self.publication.session_id:
                raise ValueError("Stale Utilization session ID")
            if self.process is None or self.publication.phase not in ACTIVE_PHASES:
                raise ValueError("No active Utilization session")
            if op == "stop":
                # SIGINT reaches an owner blocked in startup as well as a live
                # loop. RadioSession and run_live retain their cleanup paths.
                if self.publication.phase is not SessionPhase.STOPPING:
                    self.process.send_signal(signal.SIGINT)
                    self._update(phase=SessionPhase.STOPPING)
            else:
                enabled = request.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Logging enabled must be boolean")
                if self.publication.phase is SessionPhase.STOPPING:
                    raise ValueError("Session is stopping")
                config = self.publication.config
                if enabled and config.stats_csv is None and config.beacons_jsonl is None:
                    raise ValueError("This session has no logging formats configured")
                self._write_logging(enabled)
        else:
            raise ValueError("Unknown Utilization service operation")
        return {"version": PROTOCOL_VERSION, "ok": True, "publication": to_wire(self.publication)}

    def _start(self, config):
        session_id = uuid4().hex
        self.control_path = self.directory / f"{session_id}.logging.json"
        config = replace(config, logging_control_path=self.control_path, logging_status_path=None)
        parent, child = socket.socketpair()
        self.publication = RuntimePublication(
            session_id, self.publication.revision + 1, SessionPhase.STARTING,
            config, MetricsSnapshot.empty(),
        )
        try:
            self._write_logging(bool(config.initial_logging_enabled))
            self.process = subprocess.Popen(
                [*self.owner_command, "--owner-fd", str(child.fileno())],
                pass_fds=(child.fileno(),), start_new_session=True,
                stdin=subprocess.DEVNULL,
            )
            self.owner = FramedSocket(parent)
            self.owner.queue({"session_id": session_id, "config": to_wire(config)})
            self.owner.flush()
        except BaseException as error:
            parent.close()
            if self.process is not None:
                self.process.send_signal(signal.SIGINT)
                self.process.wait(timeout=5)
            self.process, self.owner = None, None
            self.control_path.unlink(missing_ok=True)
            self._update(phase=SessionPhase.FAILED, error=str(error))
            raise
        finally:
            child.close()

    def _write_logging(self, enabled):
        import json

        temporary = self.control_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({"logging_enabled": enabled}))
            temporary.replace(self.control_path)
        finally:
            temporary.unlink(missing_ok=True)

    def poll_owner(self):
        if self.owner is None:
            return
        try:
            self.owner.flush()
            for message in self.owner.receive():
                publication = from_wire(RuntimePublication, message.get("publication"))
                if publication.session_id != self.publication.session_id:
                    continue
                # A late RUNNING publication cannot undo an accepted stop.
                phase = publication.phase
                if self.publication.phase is SessionPhase.STOPPING and phase in ACTIVE_PHASES:
                    phase = SessionPhase.STOPPING
                self.publication = replace(
                    publication, phase=phase, revision=self.publication.revision + 1,
                )
        except (EOFError, OSError, ValueError, TypeError) as error:
            self._update(error=f"Owner publication failed: {error}")
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGINT)
        code = self.process.poll()
        if code is not None and self.owner.eof:
            if self.publication.phase in ACTIVE_PHASES:
                self._update(
                    phase=SessionPhase.FAILED, exit_code=code,
                    error=f"Capture owner exited before final publication ({code})",
                    logging=replace(self.publication.logging, active=False),
                )
            self.owner.sock.close()
            self.process, self.owner = None, None
            self.control_path.unlink(missing_ok=True)
            self.control_path = None

    def shutdown(self):
        self.poll_owner()
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        if self.publication.phase in ACTIVE_PHASES:
            self._update(phase=SessionPhase.STOPPING)
        deadline = time.monotonic() + OWNER_STOP_TIMEOUT
        while self.process is not None and time.monotonic() < deadline:
            self.poll_owner()
            time.sleep(0.02)
        if self.process is not None:
            self.process.kill()
            self.process.wait(timeout=5)
            self.poll_owner()
            raise RuntimeError("Utilization owner did not stop; forced exit cannot guarantee radio restoration")


def run_owner(fd: int, *, runner=None) -> int:
    sock = socket.socket(fileno=fd)
    sock.settimeout(5)
    initial = receive_blocking(sock)
    config = from_wire(LiveSessionConfig, initial["config"])
    channel = FramedSocket(sock)
    latest = None

    def publish(publication):
        nonlocal latest
        latest = publication

    def poll():
        nonlocal latest
        channel.receive()  # The broker uses signals for explicit stop controls.
        if channel.eof:
            runtime.stop(initial["session_id"])
        if latest is not None:
            channel.queue({"publication": to_wire(latest)})
            latest = None
        channel.flush()

    runtime = ManagedRuntime(runner=runner, on_publication=publish, poll_controls=poll)
    result = 0
    try:
        result = runtime.run(config, session_id=initial["session_id"])
    except KeyboardInterrupt:
        result = 0
    except Exception as error:
        print(f"Utilization capture failed: {error}", file=sys.stderr, flush=True)
        result = 1
    finally:
        # Capture cleanup has finished. A bounded final drain only affects
        # owner exit, never live analysis. A dead broker cannot hold resources.
        try:
            poll()
            deadline = time.monotonic() + 5
            while channel.writing and time.monotonic() < deadline:
                channel.flush()
                time.sleep(0.005)
        except (EOFError, OSError, ValueError):
            pass
        sock.close()
    return result


def serve(socket_path: Path, *, shutdown_requested=None, owner_command=None):
    socket_path = socket_path.absolute()
    directory = socket_path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if directory.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Utilization socket directory must be private and owned by this user")
    lock_fd = os.open(directory / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    selector = selectors.DefaultSelector()
    broker = ServiceBroker(directory, owner_command=owner_command)
    clients = {}
    bound = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if socket_path.exists() or socket_path.is_symlink():
            info = socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Refusing to replace an unrelated Utilization socket path")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(socket_path))
                except ConnectionRefusedError:
                    socket_path.unlink()
                else:
                    raise ValueError("A Utilization listener is already active")
        listener.bind(str(socket_path))
        bound = True
        socket_path.chmod(0o600)
        listener.listen(MAX_CLIENTS)
        listener.setblocking(False)
        selector.register(listener, selectors.EVENT_READ)
        while shutdown_requested is None or not shutdown_requested():
            broker.poll_owner()
            for key, mask in selector.select(0.05):
                if key.fileobj is listener:
                    conn, _ = listener.accept()
                    if len(clients) >= MAX_CLIENTS:
                        conn.close()
                        continue
                    clients[conn] = [FramedSocket(conn), time.monotonic() + CLIENT_TIMEOUT, False]
                    selector.register(conn, selectors.EVENT_READ)
                    continue
                conn = key.fileobj
                channel, deadline, answered = clients[conn]
                try:
                    if not answered:
                        messages = channel.receive()
                        if messages:
                            if len(messages) != 1:
                                raise ValueError("One request per connection is required")
                            try:
                                response = broker.dispatch(messages[0])
                            except (ValueError, TypeError, OSError) as error:
                                response = {"version": PROTOCOL_VERSION, "ok": False, "error": str(error)}
                            channel.queue(response)
                            clients[conn][2] = True
                            selector.modify(conn, selectors.EVENT_WRITE)
                    if mask & selectors.EVENT_WRITE:
                        channel.flush()
                except (EOFError, OSError, ValueError, TypeError):
                    channel.eof = True
            for conn, (channel, deadline, answered) in list(clients.items()):
                if channel.eof or time.monotonic() >= deadline or (answered and not channel.writing):
                    selector.unregister(conn)
                    conn.close()
                    del clients[conn]
    finally:
        try:
            broker.shutdown()
        finally:
            for conn in clients:
                conn.close()
            selector.close()
            listener.close()
            if bound:
                socket_path.unlink(missing_ok=True)
            os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wlanpi-utilization serve", description="Run the Utilization local service.")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--owner-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.owner_fd is not None:
        return run_owner(args.owner_fd)
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        serve(args.socket, shutdown_requested=lambda: stopping)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
