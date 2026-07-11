# WLANPi Beacon Live

Python CLI for live and replayed WLAN beacon QBSS channel-utilization analysis.

Requires Python 3.9 or newer.

Replay mode is hardware-free: it parses saved TShark TSV rows and saved
`iw dev <iface> survey dump` text, then aggregates beacon records into
per-second stats. A minimal WLAN Pi live mode is also available for terminal
testing.

## Install

```bash
pip install -e ".[dev]"
```

## Run tests

```bash
pytest
```

## CLI

```bash
beacon-live --help
beacon-live replay --input samples/tshark_qbss_sample.tsv
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --channel 36
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --frequency-mhz 5975
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --band 6 --channel 5
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36 --survey-debug
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --channel 36 --stats-csv --beacons-jsonl --log-dir logs
```

`wlanpi-beacon-live` is the on-device foreground launcher intended for future
WLANPi menu integration. It always runs live mode, accepts the same options as
`beacon-live live`, and exits cleanly on Ctrl-C. A front-panel menu should launch
the installed executable by absolute path, for example:

```text
/home/<user>/WLANPi-Utilization/.venv/bin/wlanpi-beacon-live --iface wlan0 --channel 36
```

The launcher is only a stable process boundary. Capture, aggregation, logging,
and dashboard behavior remain in the core package; it does not contain
display-specific hardware integration or observed-client tracking.

The replay command prints tab-separated per-second stats from a saved TShark
sample file with these fields:

```text
frame.time_epoch, wlan.ssid, wlan.bssid, wlan.qbss.cu, wlan.qbss.scount, wlan.qbss.adc
```

QBSS channel utilization is converted from raw `0-255` values to percent with:

```text
raw / 255 * 100
```

The live command's core path configures the selected interface for monitor
mode, tunes with 20 MHz width, starts TShark, and prints AP-reported QBSS
terminal stats in a rolling 120-second dashboard that redraws once per second
and displays local wall-clock time. Each table row's QBSS CU min/mean/max is
computed from AP reports observed during that second. The bar graph plots only
the maximum from each second, and the rolling min/mean/max summary is computed
from that same max-only series. Seconds without a QBSS maximum are graph gaps
and are excluded from the numeric summary. The first two dashboard cycles are
an explicit warm-up period and are excluded from the graph and rolling summary.

Live mode does not require local survey counters. Use
`--channel` for channel numbers and `--frequency-mhz` for explicit center
frequency. For 6 GHz PSC channel 5, use either `--frequency-mhz 5975` or
`--band 6 --channel 5`.

Local survey CU is driver-dependent and strictly opt-in. Add `--local-cu` to poll
`iw dev <iface> survey dump` once per second and populate `local_survey_cu` in
the terminal dashboard. Without that flag, local survey CU is neither measured
nor displayed. Add `--survey-debug` to print the selected survey frequency plus
active/busy counter deltas to stderr. When polling is enabled, an `unavailable`
value means the adapter/driver did not provide usable counters for the tuned
frequency; it does not indicate a beacon-capture failure. Unsupported, missing,
or unparseable survey output produces a warning while beacon capture, dashboard
updates, and logging continue normally.

Live logging is optional. Use `--stats-csv` and/or `--beacons-jsonl` to enable
the main outputs, and optionally set their designated folder with `--log-dir`
(default: `logs`). These flags do not accept filenames. The app creates a shared
timestamped run prefix and writes names such as
`beacon_live_20260710T183045123456Z_stats.csv` and
`beacon_live_20260710T183045123456Z_beacons.jsonl`. Files remain flushed per
stats row or beacon record. Both formats include capture start time, interface,
channel, 20 MHz channel width, and explicit frequency/band metadata when
supplied.
