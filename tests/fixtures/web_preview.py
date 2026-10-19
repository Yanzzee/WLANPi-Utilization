"""Local, synthetic web preview using the real analyzer and production renderer.

No capture commands run. This fixture is for browser QA, not live deployment.
"""
import argparse
import json
import math
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from beacon_live.analyzer import Analyzer
from beacon_live.channel import ChannelDefinition, ChannelWidth
from beacon_live.models import FrameRecord, MetricsSnapshot
from beacon_live.runtime import LiveSessionConfig, LoggingStatus, RuntimePublication, SessionPhase
from beacon_live.service_client import ServiceError
from beacon_live.web import create_app


def preview_snapshot():
    now = int(time.time())
    analyzer = Analyzer(streaming=True, target_primary_frequency_mhz=5180)
    definition = ChannelDefinition(5180, ChannelWidth.MHZ80, 5210, primary_channel=36)
    for offset in range(120):
        second = now - 120 + offset
        for index, (name, rssi, stations) in enumerate((
            ("Classroom East", -42, 31), ("Faculty", -43, 12), ("Guest network", -68, 75),
        )):
            raw = int(72 + index * 24 + 28 * math.sin(offset / 9) + 15 * math.sin(offset / 3)) if index < 2 else None
            analyzer.ingest(FrameRecord(
                timestamp=second + .1 + index * .001, bssid=f"02:00:00:00:00:{index + 16:02x}",
                ssid=name, rssi_dbm=rssi, frame_type=0, frame_subtype=8, retry_flag=False,
                receiver_address="ff:ff:ff:ff:ff:ff", qbss_cu_raw=raw,
                qbss_cu_percent=raw / 255 * 100 if raw is not None else None,
                qbss_station_count=stations + int(5 * math.sin(offset / 16)) if stations else None,
                qbss_admission_capacity=24000 if raw is not None else None, beacon_interval_tu=100,
                ap_name="AP-East-01" if index < 2 else "AP-Hall-02", vendor="Example",
                channel_definition=definition,
            ), publish_snapshot=False)
        count = 400 if offset == 119 else 30
        for frame in range(count):
            analyzer.ingest(FrameRecord(
                timestamp=second + .3 + frame * .0005, bssid="02:00:00:00:00:10",
                frame_type=2, frame_subtype=0,
                retry_flag=frame == 0 if offset == 119 else frame < 2 + int(2 * abs(math.sin(offset / 10))),
                receiver_address="02:00:00:00:00:10", transmitter_address=f"02:10:00:00:00:{frame % 8 + 1:02x}",
            ), publish_snapshot=False)
        analyzer.ingest(FrameRecord(
            timestamp=second + .85, bssid="02:00:00:00:00:11", frame_type=2, frame_subtype=0,
            retry_flag=True, receiver_address="02:00:00:00:00:11", transmitter_address="02:10:00:00:00:10",
        ), publish_snapshot=False)
        analyzer.publish_capture_complete(32 + 12 * math.sin(offset / 12))
    analyzer.ingest(FrameRecord(now + .3, frame_type=1), publish_snapshot=False)
    analyzer.publish_capture_complete(36.2)
    snapshot = analyzer.snapshot
    history = list(snapshot.history)
    for index in (35, 36, 72):
        history[index] = replace(MetricsSnapshot.empty().current, second=history[index].second, sample_available=False)
    return replace(snapshot, history=tuple(history))


class PreviewService:
    def __init__(self, directory):
        self.lock = threading.RLock()
        self.snapshot = preview_snapshot()
        self.publication = RuntimePublication(
            uuid4().hex, 1, SessionPhase.RUNNING,
            LiveSessionConfig(band="5", channel="36", local_cu=True,
                              stats_csv=directory / "utilization_current_stats.csv",
                              beacons_jsonl=directory / "utilization_current_beacons.jsonl"), self.snapshot,
            completed_stats=tuple(row for row in self.snapshot.history if row.sample_available),
            collecting=False, capture_width="80 MHz",
            capture_definition=ChannelDefinition(5180, ChannelWidth.MHZ80, 5210, primary_channel=36),
            capture_definition_verified=True,
            logging=LoggingStatus(True, (directory / "utilization_current_stats.csv", directory / "utilization_current_beacons.jsonl")),
        )
        self.records = []
        for suffix, content in (("stats.csv", "second,retry_percent\n1000,0.25\n"),
                                ("beacons.jsonl", '{"ssid":"Classroom East"}\n')):
            path = directory / f"utilization_closed_{suffix}"
            path.write_text(content)
            info = path.stat()
            self.records.append(dict(id=uuid4().hex, path=str(path), device=info.st_dev, inode=info.st_ino,
                                     size_bytes=info.st_size, mtime_ns=info.st_mtime_ns, closed_at=time.time() - 180,
                                     session_id=self.publication.session_id))

    def read(self):
        with self.lock:
            return self.publication

    def request(self, operation, **values):
        with self.lock:
            if operation == "start":
                if self.publication.phase is SessionPhase.RUNNING:
                    raise ServiceError("A Utilization session owns the radio")
                self.publication = replace(self.publication, config=values["config"], session_id=uuid4().hex,
                                           phase=SessionPhase.RUNNING, revision=self.publication.revision + 1)
            return self.publication

    def stop(self, identifier):
        with self.lock:
            if identifier != self.publication.session_id:
                raise ServiceError("Stale Utilization session ID")
            self.publication = replace(self.publication, phase=SessionPhase.STOPPED,
                                       logging=replace(self.publication.logging, active=False),
                                       revision=self.publication.revision + 1)
            return self.publication

    def set_logging(self, identifier, enabled):
        with self.lock:
            if identifier != self.publication.session_id:
                raise ServiceError("Stale Utilization session ID")
            self.publication = replace(self.publication, logging=replace(self.publication.logging, active=enabled),
                                       revision=self.publication.revision + 1)
            return self.publication

    def files(self):
        return self.records


def main():
    from waitress import serve

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--export", type=Path, help="Write synthetic HTML/status fixtures for frontend logic checks and exit.")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="utilization-preview-", dir="/private/tmp") as folder:
        directory = Path(folder)
        service = PreviewService(directory)
        if args.export:
            app = create_app(client=service, log_directory=directory, proxy_token="a" * 64)
            client = app.test_client()
            headers = {"X-Utilization-Proxy-Token": "a" * 64}
            args.export.mkdir(parents=True, exist_ok=True)
            (args.export / "preview.html").write_text(client.get("/utilization/", headers=headers).text)
            (args.export / "status.json").write_text(json.dumps(client.get("/utilization/api/status", headers=headers).json))
            print("Synthetic fixtures:", args.export)
            return
        app = create_app(client=service, log_directory=directory)
        print("Synthetic preview:", f"http://127.0.0.1:{args.port}/utilization/", flush=True)
        serve(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
