# WLANPi Beacon Live

Python CLI scaffolding for replaying WLAN beacon QBSS channel utilization data.

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
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --frequency-mhz 5975
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --band 6 --channel 5
```

The replay command prints tab-separated per-second stats from a saved TShark
sample file with these fields:

```text
frame.time_epoch, wlan.ssid, wlan.bssid, wlan.qbss.cu, wlan.qbss.scount, wlan.qbss.adc
```

QBSS channel utilization is converted from raw `0-255` values to percent with:

```text
raw / 255 * 100
```

The live command configures the selected interface for monitor mode, tunes with
20 MHz width, starts TShark, polls `iw dev <iface> survey dump` once per second,
and prints terminal stats. Use `--channel` for channel numbers and
`--frequency-mhz` for explicit center frequency. For 6 GHz PSC channel 5, use
either `--frequency-mhz 5975` or `--band 6 --channel 5`. AP-reported QBSS CU and
local survey CU are kept as separate metrics.
