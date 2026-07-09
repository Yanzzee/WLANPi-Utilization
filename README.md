# WLANPi Beacon Live

Python CLI scaffolding for replaying WLAN beacon QBSS channel utilization data.

This first step is intentionally hardware-free: it parses saved TShark TSV rows
and saved `iw dev <iface> survey dump` text, then aggregates beacon records into
per-second stats. Live capture, sudo use, TShark subprocesses, and `iw`
subprocesses are not implemented yet.

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
