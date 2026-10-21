"""Bounded live beacon export; filesystem work lives in a disposable process."""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import suppress
import fcntl
import math
import multiprocessing
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import threading
import time
from uuid import uuid4

from beacon_live.models import ClosedLog
from beacon_live.pcapng import BeaconFilter, ExportStats, SHB, IDB

MIB = 1024 * 1024
MAX_CONTEXT_BYTES = 256 * 1024
OWNED_NAME = re.compile(r"utilization_beacons_[0-9a-f]{32}_[0-9]{6}\.pcapng\Z")
PARTIAL_NAME = re.compile(r"utilization_beacons_[0-9a-f]{32}_[0-9]{6}\.pcapng\.partial\Z")


@dataclass(frozen=True)
class ExportLimits:
    segment_bytes: int = 64 * MIB
    rotation_seconds: float = 300.0
    retained_bytes: int = 512 * MIB
    retained_files: int = 16
    min_free_bytes: int = 256 * MIB
    queue_bytes: int = 8 * MIB
    queue_records: int = 64

    def validate(self):
        if (min(self.segment_bytes, self.retained_bytes, self.retained_files,
                self.queue_bytes, self.queue_records) <= 0 or self.rotation_seconds <= 0
                or self.min_free_bytes < 0 or self.segment_bytes > self.retained_bytes
                or not math.isfinite(self.rotation_seconds)
                or self.retained_files > 256 or self.queue_records > 4096
                or self.queue_bytes > 256 * MIB):
            raise ValueError("Invalid beacon export limits")


@dataclass(frozen=True)
class BeaconExportStatus:
    requested: bool = False
    active: bool = False
    path: Path | None = None
    exported_beacons: int = 0  # Successfully closed segments only.
    active_beacons: int = 0
    skipped_beacons: int = 0
    queue_full: int = 0
    queued_bytes: int = 0
    retained_bytes: int = 0
    deleted_segments: int = 0
    incomplete: bool = False
    error: str | None = None
    selection: ExportStats = ExportStats()


class RotatingBeaconWriter:
    """Worker-only storage. Closed, reserved-name files form the retention pool.

    This directory is a dedicated, trusted export directory. Symlinks, foreign
    owners, unrelated names and partial files are never retention candidates.
    """
    def __init__(self, directory: Path, limits: ExportLimits, *, clock=time.monotonic,
                 wall_clock=time.time, disk_usage=shutil.disk_usage):
        limits.validate()
        self.directory, self.limits = directory, limits
        self.clock, self.wall_clock, self.disk_usage = clock, wall_clock, disk_usage
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise OSError("Beacon export directory must not be a symlink")
        self.lease = os.fdopen(os.open(directory / ".utilization-export.lock",
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lease.close()
            raise OSError("Beacon export directory already has a writer") from None
        self.run = uuid4().hex
        self.sequence = 0
        self.stream = None
        self.path = None
        self.context = ()
        self.size = self.active_beacons = self.exported_beacons = self.skipped_beacons = 0
        self.deleted_segments = 0
        self.closed: list[ClosedLog] = []
        self.retained = []
        for path in directory.iterdir():
            if PARTIAL_NAME.fullmatch(path.name):
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                    path.unlink()  # The exclusive lease proves no writer owns it.
            if OWNED_NAME.fullmatch(path.name):
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                    self.retained.append((path, info))
        self.retained.sort(key=lambda item: (item[1].st_mtime_ns, item[0].name))
        self.started = self.clock()
        self._budget(0, opening=False)

    @property
    def retained_bytes(self):
        return sum(info.st_size for _, info in self.retained) + self.size

    def _budget(self, incoming, *, opening):
        while self.retained and (
            self.retained_bytes + incoming > self.limits.retained_bytes
            or len(self.retained) + int(self.stream is not None or opening) > self.limits.retained_files
        ):
            path, expected = self.retained.pop(0)
            try:
                current = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino, current.st_uid) != (
                    expected.st_dev, expected.st_ino, os.geteuid()):
                raise OSError("Retained beacon file identity changed")
            path.unlink()
            self.closed = [item for item in self.closed if item.path != path]
            self.deleted_segments += 1
        if self.retained_bytes + incoming > self.limits.retained_bytes:
            raise OSError("Beacon export retention byte limit reached")
        if self.disk_usage(self.directory).free - incoming < self.limits.min_free_bytes:
            raise OSError("Beacon export stopped: low disk space")

    def maintain(self):
        if self.stream is not None and self.clock() - self.started >= self.limits.rotation_seconds:
            self.close_segment()

    def write(self, context: tuple[bytes, ...], packet: bytes):
        self.maintain()
        if self.stream is not None and (context != self.context or self.size + len(packet) > self.limits.segment_bytes):
            self.close_segment()
        header_size = sum(map(len, context)) if self.stream is None else 0
        if header_size + len(packet) > self.limits.segment_bytes:
            raise OSError("Beacon and interface context exceed the segment size limit")
        self._budget(header_size + len(packet), opening=self.stream is None)
        if self.stream is None:
            self.sequence += 1
            if self.sequence > 999999:
                raise OSError("Beacon export segment sequence exhausted")
            final = self.directory / f"utilization_beacons_{self.run}_{self.sequence:06d}.pcapng"
            self.path = final.with_suffix(".pcapng.partial")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self.stream = os.fdopen(fd, "wb")
            self.context = context
            self.started = self.clock()
            for header in context:
                self._write(header)
        self._write(packet)
        self.active_beacons += 1

    def _write(self, data):
        pending = memoryview(data)
        while pending:
            count = self.stream.write(pending)
            if count is None or not 0 < count <= len(pending):
                raise OSError("Beacon export writer made no progress")
            self.size += count
            pending = pending[count:]

    def close_segment(self):
        if self.stream is None:
            return
        self.stream.flush()
        info = os.fstat(self.stream.fileno())
        self.stream.close()
        self.stream = None
        final = self.path.with_suffix("")
        os.link(self.path, final, follow_symlinks=False)  # Never overwrite another file.
        self.path.unlink()
        self.closed.append(ClosedLog(final, info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, self.wall_clock()))
        self.closed = self.closed[-256:]
        self.retained.append((final, info))
        self.exported_beacons += self.active_beacons
        self.path = None
        self.size = self.active_beacons = 0

    def abort(self):
        self.skipped_beacons += self.active_beacons
        self.active_beacons = 0
        try:
            if self.stream is not None:
                with suppress(OSError):
                    self.stream.close()
        finally:
            self.stream = None
            if self.path is not None:
                with suppress(OSError):
                    self.path.unlink(missing_ok=True)
            self.path = None
            self.size = 0
            self.lease.close()

    def close(self):
        try:
            self.close_segment()
        finally:
            self.lease.close()


def _latest(outbox, value, *, final=False):
    try:
        if final:
            outbox.put(value, timeout=0.2)  # Only during worker exit, never source delivery.
        else:
            outbox.put_nowait(value)
    except queue.Full:
        # Keep the old publication until the parent consumes it. Never wait on
        # a consumer; the next maintenance tick will publish newer state.
        pass


def _export_worker(records, reports, control, pending_bytes, directory, limits, writer_factory, parent_pid):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    writer = None
    generation = -1
    enabled = False
    error = None
    incomplete = False
    skipped = committed = deleted = retained_bytes = 0
    closed = ()
    last_report = 0.0

    def finish(abort=False):
        nonlocal writer, committed, skipped, closed, deleted, retained_bytes
        if writer is not None:
            if abort:
                writer.abort()
            else:
                writer.close()
            committed += writer.exported_beacons
            skipped += writer.skipped_beacons
            deleted += writer.deleted_segments
            retained_bytes = writer.retained_bytes
            closed = tuple((list(closed) + writer.closed)[-256:])
            writer = None

    def report(force=False):
        nonlocal last_report
        if force or time.monotonic() - last_report >= 0.1:
            _latest(reports, (BeaconExportStatus(
                active=enabled and error is None, path=writer.path if writer else None,
                exported_beacons=committed + (writer.exported_beacons if writer else 0),
                active_beacons=writer.active_beacons if writer else 0,
                skipped_beacons=skipped + (writer.skipped_beacons if writer else 0),
                retained_bytes=writer.retained_bytes if writer else retained_bytes,
                deleted_segments=deleted + (writer.deleted_segments if writer else 0),
                incomplete=incomplete, error=error,
            ), tuple((list(closed) + (writer.closed if writer else []))[-256:])), final=force)
            last_report = time.monotonic()

    def synchronize():
        nonlocal generation, enabled, error, writer
        with control.get_lock():
            desired_generation, desired_enabled, stopping = control[:]
        if desired_generation != generation:
            finish()
            generation, enabled, error = desired_generation, bool(desired_enabled), None
            if enabled:
                writer = writer_factory(directory, limits)
        return stopping

    try:
        while True:
            try:
                stopping = synchronize()
                orphaned = os.getppid() != parent_pid
                try:
                    epoch, context, packet, size = records.get(timeout=0.05)
                except queue.Empty:
                    with pending_bytes.get_lock():
                        drained = pending_bytes.value == 0
                    if orphaned or (stopping and drained):
                        break
                else:
                    with pending_bytes.get_lock():
                        pending_bytes.value -= size
                    # A control may change while get() waits. Admit a packet
                    # from the new generation rather than discard its first
                    # beacon using a stale pre-wait control value.
                    synchronize()
                    if enabled and error is None and epoch == generation:
                        try:
                            writer.write(context, packet)
                        except OSError:
                            skipped += 1
                            raise
                    else:
                        skipped += 1
                if writer is not None:
                    writer.maintain()
            except OSError as exc:
                error, incomplete, enabled = str(exc), True, False
                try:
                    finish(abort=True)
                except OSError:
                    writer = None
            report()
        try:
            finish()
        except OSError as exc:
            error, incomplete = str(exc), True
            try:
                finish(abort=True)
            except OSError:
                pass
        enabled = False
        # Parent drains reports while joining; give the final publication its
        # own slot rather than let an older active state hide closed files.
        while True:
            try:
                reports.get_nowait()
            except queue.Empty:
                break
        report(force=True)
    finally:
        records.close()
        reports.close()
        if os.getppid() == parent_pid:
            reports.join_thread()
        else:
            reports.cancel_join_thread()


class BeaconExport:
    """Nonblocking producer, bounded queue and independently stoppable writer.

    Only ``offer`` runs on source delivery. It classifies raw frame headers,
    never computes metrics, and never performs filesystem or blocking queue I/O.
    """
    def __init__(self, directory: Path, *, limits=ExportLimits(), enabled=False,
                 writer_factory=RotatingBeaconWriter):
        limits.validate()
        self.limits = limits
        self.selector = BeaconFilter()
        self.context: tuple[bytes, ...] = ()
        self.context_size = 0
        self.lock = threading.RLock()
        self.closed = False
        self.reports_closed = False
        self.requested = bool(enabled)
        self.generation = 0
        self.dropped = self.queue_full = 0
        self.pending_value = 0
        self.error = None
        self.status = BeaconExportStatus()
        self.closed_logs: tuple[ClosedLog, ...] = ()
        ctx = multiprocessing.get_context("spawn")
        self.records = ctx.Queue(maxsize=limits.queue_records)
        self.reports = ctx.Queue(maxsize=2)
        self.control = ctx.Array("q", [0, int(enabled), 0])
        self.pending_bytes = ctx.Value("q", 0)
        self.worker = ctx.Process(target=_export_worker,
            args=(self.records, self.reports, self.control, self.pending_bytes,
                  directory, limits, writer_factory, os.getpid()), name="utilization-beacon-export", daemon=True)
        self.worker.start()

    def set_enabled(self, enabled: bool):
        with self.lock:
            if self.closed or enabled == self.requested:
                return
            self.requested = enabled
            self.generation += 1
            lock = self.control.get_lock()
            if not lock.acquire(timeout=0.01):
                self.error = "Beacon export control unavailable"
                return
            try:
                self.control[0], self.control[1] = self.generation, int(enabled)
            finally:
                lock.release()

    def offer(self, block):
        with self.lock:
            if self.closed or self.error is not None:
                return
            try:
                selected = self.selector.select(block)
                if block.kind == SHB:
                    self.context = (selected,)
                    self.context_size = len(selected)
                    if self.context_size > MAX_CONTEXT_BYTES:
                        raise ValueError("Beacon export section context exceeds its byte limit")
                elif block.kind == IDB:
                    self.context_size += len(selected)
                    if self.context_size > MAX_CONTEXT_BYTES:
                        raise ValueError("Beacon export interface context exceeds its byte limit")
                    self.context += (selected,)
                elif selected is not None and self.requested and self.worker.is_alive():
                    size = self.context_size + len(selected)
                    lock = self.pending_bytes.get_lock()
                    if not lock.acquire(block=False):
                        self.dropped += 1
                        self.queue_full += 1
                        return
                    try:
                        if self.pending_bytes.value + size > self.limits.queue_bytes:
                            self.dropped += 1
                            self.queue_full += 1
                            return
                        try:
                            self.records.put_nowait((self.generation, self.context, selected, size))
                        except queue.Full:
                            self.dropped += 1
                            self.queue_full += 1
                        else:
                            self.pending_bytes.value += size
                    finally:
                        lock.release()
            except (ValueError, OSError) as exc:
                # A metadata/selection failure disables only this branch.
                self.error = str(exc)
                if self.control.get_lock().acquire(block=False):
                    try:
                        self.control[0] += 1
                        self.control[1] = 0
                    finally:
                        self.control.get_lock().release()

    def read(self):
        if not self.closed and not self.worker.is_alive():
            self.error = self.error or "Beacon export worker exited unexpectedly"
            self.reports_closed = True
        try:
            while not self.reports_closed:
                self.status, self.closed_logs = self.reports.get_nowait()
        except (queue.Empty, ValueError):
            pass
        except (EOFError, OSError):
            self.error = self.error or "Beacon export worker status unavailable"
            self.reports_closed = True
        if self.pending_bytes.get_lock().acquire(block=False):
            try:
                self.pending_value = self.pending_bytes.value
            finally:
                self.pending_bytes.get_lock().release()
        pending = self.pending_value
        error = self.error or self.status.error
        if not self.closed and not self.worker.is_alive():
            error = error or "Beacon export worker exited unexpectedly"
        return replace(self.status, requested=self.requested,
            active=self.status.active and error is None and not self.closed,
            skipped_beacons=self.status.skipped_beacons + self.dropped,
            queue_full=self.queue_full, queued_bytes=pending,
            selection=self.selector.stats, error=error,
            incomplete=self.status.incomplete or error is not None)

    def close(self, timeout=2.0):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.control.get_lock().acquire(timeout=0.01):
                try:
                    self.control[2] = 1
                finally:
                    self.control.get_lock().release()
        deadline = time.monotonic() + timeout
        while self.worker.is_alive() and time.monotonic() < deadline:
            self.read()
            self.worker.join(timeout=0.02)
        if self.worker.is_alive():
            self.worker.terminate()
            self.worker.join(timeout=0.5)
            self.error = "Beacon export writer did not stop in time; active output is incomplete"
            self.reports_closed = True  # A killed Queue producer may leave a partial message.
        elif self.worker.exitcode not in (None, 0):
            self.error = self.error or "Beacon export worker exited unexpectedly"
            self.reports_closed = True
        self.read()
        self.records.cancel_join_thread()
        self.records.close()
        self.reports_closed = True
        self.reports.close()
