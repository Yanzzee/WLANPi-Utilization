#!/usr/bin/env python3
"""Hardware-free parser/analyzer throughput check with exact metric assertions.

This excludes TShark, USB/radio capture, IPC, HTTP and rendering. Run on each
Pi to compare this stage; use a real capture to diagnose end-to-end losses.
"""
import argparse
import json
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beacon_live.analyzer import Analyzer
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES, TSHARK_LIVE_OPTIONAL_FRAME_FIELD_NAMES, parse_tshark_live_record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--frames-per-second", type=int, default=2000)
    args = parser.parse_args()
    if args.seconds < 1 or args.frames_per_second < 40 or args.frames_per_second % 40:
        parser.error("Use positive seconds and a frame count divisible by 40")
    fields = TSHARK_FRAME_FIELD_NAMES + TSHARK_LIVE_OPTIONAL_FRAME_FIELD_NAMES
    ap = "02:00:00:00:00:10"
    analyzer = Analyzer(streaming=True)
    completed = []

    def consume(values):
        frames = parse_tshark_live_record("\t".join(str(values.get(name, "")) for name in fields),
                                          field_names=fields, band="5")
        assert len(frames) == 1
        analyzer.ingest(frames[0], publish_snapshot=False)

    start = time.perf_counter()
    cpu = time.process_time()
    for offset in range(args.seconds):
        second = 1000 + offset
        consume({"frame.time_epoch": second + .01, "wlan.fc.type": 0, "wlan.fc.subtype": 8,
            "wlan.bssid": ap, "wlan.ta": ap, "wlan.ra": "ff:ff:ff:ff:ff:ff",
            "wlan.ssid": "Benchmark", "wlan.qbss.cu": 128, "wlan.qbss.scount": 8,
            "radiotap.dbm_antsignal": -40, "wlan.fixed.beacon": 100, "radiotap.flags.badfcs": "False"})
        for index in range(args.frames_per_second):
            consume({"frame.time_epoch": second + .1 + index / args.frames_per_second * .8,
                "wlan.fc.type": 2, "wlan.fc.subtype": 8, "wlan.fc.retry": int(index % 5 == 0),
                "wlan.bssid": ap, "wlan.ta": ap, "wlan.ra": f"02:00:00:00:01:{index % 8:02x}",
                "wlan.sa": "02:44:00:00:00:01", "radiotap.flags.badfcs": "False", "wlan.fcs.status": 2})
        completed.extend(analyzer.publish_capture_complete(None))
    # A final ordered capture watermark closes the last measured second;
    # never finalize a live bucket from the benchmark's wall clock.
    consume({"frame.time_epoch": 1000 + args.seconds + .2,
             "wlan.fc.type": 1, "wlan.fc.subtype": 13, "wlan.ra": ap})
    completed.extend(analyzer.publish_capture_complete(None))
    elapsed = time.perf_counter() - start
    assert len(completed) == args.seconds
    assert all(row.retry_percent == 20 and row.unique_client_mac_count == 8
               and row.unique_bssid_count == 1 and row.qbss_station_count_sum == 8 for row in completed)
    count = args.seconds * (args.frames_per_second + 1) + 1
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"stage": "synthetic row generation + parsing + streaming analysis + publication",
        "frames": count, "capture_seconds": args.seconds, "elapsed_seconds": round(elapsed, 3),
        "cpu_seconds": round(time.process_time() - cpu, 3), "frames_per_second": round(count / elapsed),
        "max_rss_mib": round(rss / (1024 ** 2 if sys.platform == "darwin" else 1024), 1),
        "history_rows": len(analyzer.snapshot.history), "metrics_verified": True}, indent=2))


if __name__ == "__main__":
    main()
