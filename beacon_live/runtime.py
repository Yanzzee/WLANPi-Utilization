"""Managed ownership and immutable publications for the existing live pipeline.

Run the owner on the capture process's main thread. Consumers may read/wait
from other threads; they never run capture or renderer code on the owner.
Cross-process IPC and service deployment are separate from this foundation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable
from uuid import uuid4

from beacon_live.channel import ChannelWidth
from beacon_live.log_writer import (
    DEFAULT_DISK_CHECK_INTERVAL_SECONDS,
    DEFAULT_MIN_FREE_BYTES,
    DEFAULT_ROTATION_INTERVAL_SECONDS,
)
from beacon_live.models import CaptureHealth, ClosedLog, MetricsSnapshot, SecondStats


class SessionPhase(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


ACTIVE_PHASES = frozenset({SessionPhase.STARTING, SessionPhase.RUNNING, SessionPhase.STOPPING})


@dataclass(frozen=True)
class LiveSessionConfig:
    """Explicit source/logging choices; direct capture remains the default."""

    iface: str = "auto"
    netns: str | None = None
    capture_backend: str = "direct"
    core_diagnostics_path: Path | None = None
    channel: str = "36"
    frequency_mhz: int | None = None
    band: str | None = None
    channel_width: ChannelWidth | None = None
    center_frequency1_mhz: int | None = None
    center_frequency2_mhz: int | None = None
    raw_capture_path: Path | None = None
    interval_seconds: float = 1.0
    local_cu: bool = False
    survey_debug: bool = False
    stats_csv: Path | None = None
    beacons_jsonl: Path | None = None
    logging_control_path: Path | None = None
    logging_status_path: Path | None = None
    initial_logging_enabled: bool | None = None
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    disk_check_interval_seconds: float = DEFAULT_DISK_CHECK_INTERVAL_SECONDS
    rotation_interval_seconds: float = DEFAULT_ROTATION_INTERVAL_SECONDS


@dataclass(frozen=True)
class LoggingStatus:
    active: bool = False
    paths: tuple[Path, ...] = ()
    message: str | None = None


@dataclass(frozen=True)
class RuntimePublication:
    """Atomic read of the latest snapshot, completed rows, and lifecycle state."""

    session_id: str | None
    revision: int
    phase: SessionPhase
    config: LiveSessionConfig | None
    snapshot: MetricsSnapshot
    completed_stats: tuple[SecondStats, ...] = ()
    collecting: bool = True
    capture_width: str | None = None
    logging: LoggingStatus = LoggingStatus()
    notice: str | None = None
    exit_code: int | None = None
    error: str | None = None
    closed_logs: tuple[ClosedLog, ...] = ()
    capture_health: CaptureHealth = CaptureHealth()


class SessionConflictError(RuntimeError):
    """An explicit new session cannot overlap the current owner's cleanup."""


class ManagedRuntime:
    """Own one run_live invocation; attaching/detaching is always read-only.

    ``run`` blocks until capture and resource cleanup finish. A host should
    dispatch explicit start commands to its capture main thread, not create an
    owner per browser or fork the Linux analyzer from an HTTP worker thread.
    """

    def __init__(
        self, *, runner: Callable[..., int] | None = None,
        on_publication: Callable[[RuntimePublication], None] | None = None,
        poll_controls: Callable[[], None] | None = None,
    ) -> None:
        self._runner = runner
        # Service callbacks only retain a latest value or poll bounded IPC.
        # They must never render, wait for a consumer, or perform disk I/O.
        self._on_publication = on_publication
        self._poll_controls = poll_controls
        self._condition = threading.Condition()
        self._stop_requested = threading.Event()
        self._publication = RuntimePublication(
            session_id=None,
            revision=0,
            phase=SessionPhase.IDLE,
            config=None,
            snapshot=MetricsSnapshot.empty(),
        )

    def read(self) -> RuntimePublication:
        with self._condition:
            return self._publication

    def subscribe(self) -> RuntimeConsumer:
        """Attach immediately, including while idle or after capture stopped."""
        return RuntimeConsumer(self)

    def run(self, config: LiveSessionConfig, *, session_id: str | None = None) -> int:
        """Start a new explicit session, preserving final state until this call."""
        if self._runner is None and threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Live capture must run on the capture owner's main thread")
        with self._condition:
            if self._publication.phase in ACTIVE_PHASES:
                raise SessionConflictError("A Utilization session is already active")
            session_id = session_id or uuid4().hex
            self._stop_requested.clear()
            self._publication = RuntimePublication(
                session_id=session_id,
                revision=self._publication.revision + 1,
                phase=SessionPhase.STARTING,
                config=config,
                snapshot=MetricsSnapshot.empty(),
            )
            self._condition.notify_all()
            if self._on_publication is not None:
                self._on_publication(self._publication)

        bridge = _RuntimeDashboard(self, session_id)
        try:
            runner = self._runner
            if runner is None:
                from beacon_live.live import run_live

                runner = run_live
            result = runner(**vars(config), _dashboard=bridge)
        except KeyboardInterrupt:
            self._finish(session_id, phase=SessionPhase.STOPPED, exit_code=130)
            raise
        except BaseException as error:
            self._finish(session_id, phase=SessionPhase.FAILED, error=str(error))
            raise
        else:
            self._finish(
                session_id,
                phase=SessionPhase.STOPPED if result == 0 else SessionPhase.FAILED,
                exit_code=result,
                error=None if result == 0 else f"Live capture exited with status {result}",
            )
            return result

    def stop(self, session_id: str) -> bool:
        """Request graceful owner cleanup; stale controls cannot stop a new run."""
        with self._condition:
            if (
                session_id != self._publication.session_id
                or self._publication.phase not in ACTIVE_PHASES
            ):
                return False
            self._stop_requested.set()
            if self._publication.phase is not SessionPhase.STOPPING:
                self._update_locked(phase=SessionPhase.STOPPING)
            return True

    def _update_locked(self, **changes: object) -> None:
        self._publication = replace(
            self._publication, revision=self._publication.revision + 1, **changes
        )
        self._condition.notify_all()
        if self._on_publication is not None:
            self._on_publication(self._publication)

    def _publish(self, session_id: str, **changes: object) -> None:
        with self._condition:
            if (
                session_id == self._publication.session_id
                and self._publication.phase in ACTIVE_PHASES
            ):
                self._update_locked(**changes)

    def _finish(self, session_id: str, **changes: object) -> None:
        with self._condition:
            if session_id == self._publication.session_id:
                self._update_locked(
                    logging=replace(self._publication.logging, active=False), **changes
                )

    def _capture_started(self, session_id: str) -> None:
        with self._condition:
            if (
                session_id == self._publication.session_id
                and self._publication.phase is SessionPhase.STARTING
            ):
                self._update_locked(phase=SessionPhase.RUNNING)

    def _publish_stats(self, session_id: str, rows: tuple[SecondStats, ...]) -> None:
        if not rows:
            return
        with self._condition:
            if (
                session_id != self._publication.session_id
                or self._publication.phase not in ACTIVE_PHASES
            ):
                return
            # Retain original completed rows, never wall-clock gaps or a second
            # aggregation. Missing elapsed seconds need no invented completion.
            window = self._publication.snapshot.window_seconds
            combined = self._publication.completed_stats + rows
            cutoff = combined[-1].second - window + 1
            self._update_locked(
                completed_stats=tuple(row for row in combined if row.second >= cutoff)[-window:]
            )


class RuntimeConsumer:
    """A bounded latest-publication view; no per-consumer capture or queue."""

    def __init__(self, runtime: ManagedRuntime) -> None:
        self._runtime = runtime
        self._closed = False

    def read(self) -> RuntimePublication:
        with self._runtime._condition:
            self._check_open()
            return self._runtime._publication

    def wait(
        self, after_revision: int, *, timeout: float | None = None
    ) -> RuntimePublication:
        """Return current state after a change or timeout; revisions reveal skips."""
        with self._runtime._condition:
            self._check_open()
            self._runtime._condition.wait_for(
                lambda: self._closed
                or self._runtime._publication.revision > after_revision,
                timeout=timeout,
            )
            self._check_open()
            return self._runtime._publication

    def close(self) -> None:
        with self._runtime._condition:
            self._closed = True
            self._runtime._condition.notify_all()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("Consumer is closed")


class _RuntimeDashboard:
    """Publication adapter for run_live's existing optional renderer methods."""

    poll_interval_seconds = 0.05

    def __init__(self, runtime: ManagedRuntime, session_id: str) -> None:
        self._runtime = runtime
        self._session_id = session_id

    def refresh(self, snapshot: MetricsSnapshot | None = None) -> None:
        if snapshot is not None:
            self._runtime._publish(self._session_id, snapshot=snapshot)

    def publish_final_snapshot(self, snapshot: MetricsSnapshot) -> None:
        self.refresh(snapshot)

    def publish_stats(self, rows: tuple[SecondStats, ...]) -> None:
        self._runtime._publish_stats(self._session_id, rows)

    def set_closed_logs(self, logs: tuple[ClosedLog, ...]) -> None:
        if logs != self._runtime.read().closed_logs:
            self._runtime._publish(self._session_id, closed_logs=logs)

    def set_capture_health(self, health: CaptureHealth) -> None:
        self._runtime._publish(self._session_id, capture_health=health)

    def set_capture_started(self) -> None:
        self._runtime._capture_started(self._session_id)

    def poll_input(self) -> bool:
        if self._runtime._poll_controls is not None:
            self._runtime._poll_controls()
        return self._runtime._stop_requested.is_set()

    def set_collecting(self, collecting: bool) -> None:
        self._runtime._publish(self._session_id, collecting=collecting)

    def set_capture_width(self, width: str) -> None:
        self._runtime._publish(self._session_id, capture_width=width)

    def set_logging_status(
        self, *, active: bool, paths: tuple[Path, ...] | None, message: str | None = None
    ) -> None:
        self._runtime._publish(
            self._session_id,
            logging=LoggingStatus(active=active, paths=paths or (), message=message),
        )

    def set_notice(self, message: str) -> None:
        self._runtime._publish(self._session_id, notice=message)
