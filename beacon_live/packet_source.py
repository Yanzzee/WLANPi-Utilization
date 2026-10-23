"""One full-packet source feeding the existing decoder and optional export."""
from __future__ import annotations

import os
import subprocess
import threading

from beacon_live.capture_target import as_capture_target
from beacon_live.pcapng import iter_blocks


def build_dumpcap_command(target, *, buffer_mib=16):
    # Monitor mode/tuning belong to RadioSession. No packet capture filter.
    return as_capture_target(target).command([
        "dumpcap", "-q", "-i", as_capture_target(target).interface,
        "-B", str(buffer_mib), "-s", "0", "-n", "-w", "-",
    ])


def forward_packets(source, decoder_input, exporter):
    """Forward every original block first; storage admission never waits."""
    for block in iter_blocks(source):
        view = memoryview(block.raw)
        while view:
            count = decoder_input.write(view)
            if count is None or not 0 < count <= len(view):
                raise OSError("Packet decoder input made no progress")
            view = view[count:]
        if exporter is not None:
            exporter.offer(block)


class PacketFanout:
    def __init__(self, source, decoder_input, exporter):
        self.source, self.decoder_input, self.exporter = source, decoder_input, exporter
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="utilization-packet-fanout", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            forward_packets(self.source, self.decoder_input, self.exporter)
        except (OSError, ValueError) as exc:
            self.error = f"Packet source failed: {exc}"
        finally:
            self.source.close()
            self.decoder_input.close()

    def join(self, timeout=2):
        self.thread.join(timeout)
        return not self.thread.is_alive()


class DirectCaptureProcess:
    """Popen-compatible facade for one Dumpcap source and one stdin decoder."""
    def __init__(self, target, *, field_names, exporter, popen=subprocess.Popen):
        from beacon_live.live import (BoundedStderrCollector, build_tshark_stream_command,
                                      terminate_tshark_process, TSHARK_CAPTURE_BUFFER_MIB)
        self.decoder = popen(build_tshark_stream_command(field_names=field_names),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.source = None
        self.closed = False
        try:
            self.source = popen(build_dumpcap_command(target, buffer_mib=TSHARK_CAPTURE_BUFFER_MIB),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.diagnostics = BoundedStderrCollector(self.source.stderr)
            self.diagnostics.start()
            self.fanout = PacketFanout(self.source.stdout, self.decoder.stdin, exporter)
            self.stdout, self.stderr = self.decoder.stdout, self.decoder.stderr
        except BaseException:
            if self.source is not None:
                terminate_tshark_process(self.source)
            terminate_tshark_process(self.decoder)
            raise

    @property
    def capture_error(self):
        error = self.fanout.error
        if self.source.poll() not in (None, 0) and not self.closed:
            error = error or f"Dumpcap exited with status {self.source.returncode}: {self.diagnostics.finish(timeout_seconds=0.05)}"
        return error

    def poll(self):
        result = self.decoder.poll()
        if result is not None and self.source.poll() is None:
            try:
                self.source.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        return result

    def close(self):
        if self.closed:
            return
        from beacon_live.live import terminate_tshark_process
        self.closed = True
        terminate_tshark_process(self.source)
        # Terminating the decoder also releases a producer blocked in its pipe.
        if not self.fanout.join(timeout=0.5):
            terminate_tshark_process(self.decoder)
            self.fanout.join(timeout=0.5)
        terminate_tshark_process(self.decoder)
        self.diagnostics.finish(timeout_seconds=0.2)


def core_fanout(decoder, exporter):
    """Route Core's existing transport into the same binary fan-out boundary."""
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb", buffering=0)
    try:
        fanout = PacketFanout(source, decoder.stdin, exporter)
    except BaseException:
        source.close()
        os.close(write_fd)
        raise
    return fanout, write_fd
