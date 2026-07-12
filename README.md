# WLANPi Beacon Live

Python CLI for live and replayed WLAN beacon QBSS channel-utilization analysis.

Requires Python 3.9 or newer.

Replay mode is hardware-free: it parses saved TShark TSV rows and saved
`iw dev <iface> survey dump` text, then aggregates beacon records into
per-second stats. Live mode supports terminal testing and the WLAN Pi R4
Waveshare 128x128 front-panel display.

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

`wlanpi-beacon-live` is the on-device foreground launcher used by the FPMS
adapter. It always runs live mode, accepts the same options as `beacon-live
live`, and exits cleanly on Ctrl-C or FPMS left-stick exit.

## WLAN Pi R4 Front-Panel Installation

On the WLAN Pi, clone this repository and run the normal setup checks, then
install the app and FPMS adapter:

```bash
./scripts/pi_setup.sh
sudo ./scripts/install_wlanpi_fpms.sh
```

The installer creates an application virtual environment at
`/opt/wlanpi-beacon-live`, adds `Apps > Utilization` to WLAN Pi FPMS,
adds a generic page-exit callback to FPMS, and restarts `wlanpi-fpms`. It keeps
one-time `.beacon-live.bak` copies of the two patched FPMS files. Rerun the
installer after an FPMS package upgrade because that package can replace its
own Python files.

The menu order is band, channel, then launch mode:

```text
Apps > Utilization > <band> > <channel and frequency> > Display
Apps > Utilization > <band> > <channel and frequency> > Display + Log
```

Bands are `2.4 GHz`, `5 GHz`, `6 GHz PSC`, and `6 GHz All`. PSC entries in the
full 6 GHz list are prefixed `PSC`. Selecting a launch mode starts capture.
Pressing the control stick left sends SIGINT to the foreground capture, waits
for TShark and logs to close, and returns to the FPMS menu.

The static menu follows WLAN Pi's unbonded 20 MHz channel list. The active
regulatory domain and adapter still decide whether `iw` can tune a listed
channel; a rejected tune is reported in the `wlanpi-fpms` journal.

For example, the exact Display command for 5 GHz channel 36 is:

```text
/opt/wlanpi-beacon-live/bin/wlanpi-beacon-live --iface wlan0 --band 5 --channel 36 --lcd-frame /run/wlanpi-beacon-live/display.ppm
```

`Display + Log` appends the following exact options:

```text
--stats-csv --beacons-jsonl --log-dir /var/log/wlanpi-beacon-live
```

FPMS owns the SPI display and control-stick input. Its adapter contains only
menu, child-process, and frame-copy wiring. Capture, QBSS selection,
aggregation, summaries, logging, and the 128x128 frame renderer remain in this
package. No observed-client tracking is included.

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
mode, tunes with 20 MHz width, starts TShark, and produces one selected QBSS CU
value per second. Selection considers only QBSS-bearing beacons seen in that
second, chooses the BSSID with the strongest observed RSSI, then uses the most
recent QBSS beacon from that BSSID. An RSSI tie prefers the later beacon. If
RSSI is unavailable for every candidate, recency is used as the fallback.

The R4 LCD frame is 128x128. Its centered plot is 120 pixels wide by 64 pixels
high, with one horizontal pixel per second and raw QBSS `0-255` values mapped
to 64 vertical pixels in four-value steps. New samples scroll in from the
right. Two larger text rows above the graph show band, channel, frequency,
`STA`, and `SUM`, followed by current CU and rolling minimum, average, and
maximum CU. `SUM` is the sum of the latest QBSS station counts advertised by
all BSSIDs observed during that second; `STA` is the QBSS station count
advertised by the BSSID selected as the CU source. Two larger rows below show
the selected AP's strongest RSSI, SSID, and BSSID. Long SSIDs are truncated.
The on-screen clock and exit hint are intentionally omitted; log records retain
their local timestamps. Missing CU seconds are graph gaps.

The graph and rolling min/mean/max summary use exactly the selected values in
the visible 120-second window. Missing selected values are excluded from the
numeric summary. The first completed live cycle is a silent warm-up: it is
omitted from the display and stats CSV logging. Raw beacon JSONL logging still
records every valid beacon from that cycle.

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

Both formats put local record time first, followed by interface, channel, and
resolved frequency/band metadata. Stats CSV records the selected
QBSS CU, source SSID/BSSID, and source beacon RSSI. Beacon JSONL retains every
valid beacon and its RSSI. The fixed 20 MHz width and beacon-only record type
are intentionally omitted.
