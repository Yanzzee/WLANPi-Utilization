"""Read-only evidence for the experimental Core transport and radio cleanup."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time


class CoreDiagnostics:
    def __init__(self, path: Path | None):
        self.path = path
        self.radio = None
        self.process = None
        self.last_write = 0.0
        self.report = {'phase': 'initializing', 'restoration_verified': None}

    def __enter__(self):
        return self

    def bind_radio(self, radio):
        if self.path is None:
            return
        self.radio = radio
        self.report['before'] = [asdict(state) for state in radio.states]
        self.write()  # Persist the baseline before any preparation/tuning.

    def update(self, process, *, records=0, frames=0, malformed=0, force=False):
        if self.path is None:
            return
        self.process = process
        self.report.update(
            phase='capturing' if process.session_id else 'connecting', session_id=process.session_id,
            received_bytes=process.received_bytes, forwarded_bytes=process.forwarded_bytes,
            stream_chunks=process.chunks, decoded_records=records,
            normalized_frames=frames, malformed_records=malformed,
            core_stop_confirmed=process.stop_confirmed, transport_error=process.capture_error,
        )
        if force or time.monotonic() - self.last_write >= 1:
            self.write()

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                    prefix=self.path.name + '.', suffix='.tmp', delete=False) as output:
                temporary = Path(output.name)
                output.write(json.dumps(self.report, indent=2) + '\n')
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.last_write = time.monotonic()

    def __exit__(self, exc_type, exc, traceback):
        if self.path is None:
            return False
        if self.process is not None:
            self.update(self.process, records=self.report.get('decoded_records', 0),
                        frames=self.report.get('normalized_frames', 0),
                        malformed=self.report.get('malformed_records', 0))
        after, issues = [], []
        if self.radio is not None and self.radio.states:
            for before in self.radio.states:
                try:
                    state = self.radio.snapshot(before.target)
                    after.append(asdict(state))
                    identity = ('phy', 'wdev', 'ifindex', 'address', 'mode', 'up')
                    if any(getattr(state, key) != getattr(before, key) for key in identity):
                        issues.append(f'{before.target.label}: identity, mode, or UP flag differs')
                    if before.mode == 'monitor':
                        if before.channel is None:
                            issues.append(f'{before.target.label}: original monitor tuning was unknown')
                        elif (state.channel is None or not before.channel.same_tuning(state.channel)
                              or state.no_ht != before.no_ht):
                            issues.append(f'{before.target.label}: monitor tuning differs')
                except (OSError, ValueError) as error:
                    issues.append(str(error))
        else:
            issues.append('No radio baseline was acquired')
        self.report.update(phase='finished', after=after, restoration_issues=issues,
                           restoration_verified=not issues)
        if exc is not None:
            self.report['exit_error_type'] = type(exc).__name__
        try:
            self.write()
        except OSError:
            if exc is None:
                raise
        return False
