"""Hardware-free service process exercising the real broker/owner IPC boundary."""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from beacon_live.analyzer import Analyzer
from beacon_live.channel import ChannelDefinition
from beacon_live.capture_target import CaptureTarget
from beacon_live.models import FrameRecord
from beacon_live.log_writer import LiveLogPaths, LoggingService, LogMetadata
from beacon_live.service import run_owner, serve


def runner(**options):
    if options["iface"] == "fail":
        raise ValueError("fixture startup failure")
    if options["iface"] == "crash":
        os._exit(17)
    dashboard = options["_dashboard"]
    path = options.get("stats_csv")
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".owners").open("a") as output:
            output.write(f"{os.getpid()}\n")
    dashboard.set_capture_target(CaptureTarget("fixture0", "capture"))
    dashboard.set_capture_started()
    analyzer = Analyzer(streaming=True)
    analyzer.set_history_start_second(1001)
    bssid = "02:00:00:00:00:10"
    frames = [
        FrameRecord(1000.1, bssid=bssid, frame_type=0, frame_subtype=8,
                    rssi_dbm=-40, channel_definition=ChannelDefinition(2412)),
        FrameRecord(1001.1, bssid=bssid, frame_type=0, frame_subtype=8,
                    rssi_dbm=-41, channel_definition=ChannelDefinition(2412)),
        FrameRecord(1001.2, bssid=bssid, frame_type=2, frame_subtype=0,
                    retry_flag=True, receiver_address=bssid),
        FrameRecord(1001.3, bssid=bssid, frame_type=2, frame_subtype=0,
                    retry_flag=False, receiver_address=bssid),
        FrameRecord(1002.3, bssid=bssid, frame_type=2, frame_subtype=0,
                    retry_flag=False, receiver_address=bssid),
    ]
    for frame in frames:
        analyzer.ingest(frame, publish_snapshot=False)
    completed = [row for row in analyzer.publish_capture_complete(None) if row.second > 1000]
    dashboard.publish_stats(tuple(completed))
    dashboard.set_collecting(False)
    dashboard.set_capture_width("20 MHz")
    dashboard.refresh(analyzer.snapshot)
    last_enabled = None
    logging = LoggingService(
        metadata=LogMetadata(interface="fixture", channel="1", band="2.4", frequency_mhz=2412),
        log_dir=path.parent, write_stats_csv=True, write_beacons_jsonl=options.get("beacons_jsonl") is not None,
        initial_paths=LiveLogPaths(path, options.get("beacons_jsonl")),
        min_free_bytes=options["min_free_bytes"], rotation_interval_seconds=options["rotation_interval_seconds"],
    )
    try:
        while not dashboard.poll_input():
            enabled = json.loads(options["logging_control_path"].read_text())["logging_enabled"]
            if enabled != last_enabled:
                if enabled:
                    logging.start()
                    for row in completed:
                        logging.write_stats(row)
                    for frame in frames:
                        beacon = frame.beacon_record()
                        if beacon is not None:
                            logging.write_beacon(beacon)
                else:
                    logging.stop()
                last_enabled = enabled
                dashboard.set_logging_status(active=logging.active, paths=tuple(
                    item for item in (logging.current_paths.stats_csv, logging.current_paths.beacons_jsonl)
                    if item is not None
                ) if logging.current_paths else ())
            event = logging.maintain()
            if event:
                dashboard.set_logging_status(active=logging.active, paths=tuple(
                    item for item in (logging.current_paths.stats_csv, logging.current_paths.beacons_jsonl)
                    if item is not None
                ))
            dashboard.set_closed_logs(logging.closed_logs)
            time.sleep(0.01)
    finally:
        logging.stop()
        dashboard.set_closed_logs(logging.closed_logs)
        if path:
            path.with_suffix(".restored").write_text("fixture cleanup completed")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--owner-fd", type=int)
    args = parser.parse_args()
    if args.owner_fd is not None:
        return run_owner(args.owner_fd, runner=runner)
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    serve(args.socket, shutdown_requested=lambda: stopped,
          owner_command=[sys.executable, str(Path(__file__).resolve())])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
