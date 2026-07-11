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
frame.time_epoch, wlan.ssid, wlan.bssid, wlan.qbss.cu, wlan.qbss.scount, wlan.qbss.adc, radiotap.dbm_antsignal
```

Legacy six-field replay files without RSSI remain supported, although live
captures and new smoke captures include `radiotap.dbm_antsignal`.

QBSS channel utilization is converted from raw `0-255` values to percent with:

```text
raw / 255 * 100
```

The live command's core path configures the selected interface for monitor
mode, tunes with 20 MHz width, starts TShark, and prints AP-reported QBSS
terminal stats in a rolling 120-second dashboard that redraws once per second
and displays local wall-clock time. Each per-second row contains one selected
QBSS CU value. Selection considers only QBSS-bearing beacons seen in that
second, chooses the BSSID with the strongest observed RSSI, then uses the most
recent QBSS beacon from that BSSID. An RSSI tie prefers the later beacon. If
RSSI is unavailable for every candidate, recency is used as the fallback.

The graph and rolling min/mean/max summary use exactly the selected values shown
in the visible 120-second rows. Missing selected values appear as graph gaps and
are excluded from the numeric summary. The first completed live cycle is a
silent warm-up: it is omitted from dashboard rows and stats CSV logging. Raw
beacon JSONL logging still records every valid beacon from that cycle.

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
`beacon_live_20260710T123045123456-0600_stats.csv` and
`beacon_live_20260710T123045123456-0600_beacons.jsonl`. Filenames and log-entry
timestamps use local time with the UTC offset. Files remain flushed per stats
row or beacon record.

Both formats include local capture start time, local record time, interface,
channel, and resolved frequency/band metadata. Stats CSV records the selected
QBSS CU, source SSID/BSSID, and source beacon RSSI. Beacon JSONL retains every
valid beacon and its RSSI. The fixed 20 MHz width and beacon-only record type
are intentionally omitted.
