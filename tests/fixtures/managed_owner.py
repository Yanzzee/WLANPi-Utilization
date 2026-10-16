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
from beacon_live.models import FrameRecord
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
    try:
        while not dashboard.poll_input():
            enabled = json.loads(options["logging_control_path"].read_text())["logging_enabled"]
            if enabled != last_enabled:
                dashboard.set_logging_status(active=enabled, paths=(path,) if path else ())
                last_enabled = enabled
            time.sleep(0.01)
    finally:
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
