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
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36 --stats-csv logs/stats.csv --beacons-jsonl logs/beacons.jsonl
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
terminal stats in a rolling 60-second dashboard that redraws once per second.
It does not require local survey counters. Use
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

Live logging is optional. Use `--stats-csv` for one flushed row per emitted
`SecondStats`, and `--beacons-jsonl` for one flushed JSON object per valid raw
beacon. Parent directories are created automatically. Both formats include
capture start time, interface, channel, 20 MHz channel width, and explicit
frequency/band metadata when supplied.
