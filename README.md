# WLANPi Beacon Live 1.0

WLANPi Beacon Live displays live 802.11 beacon QBSS channel utilization on a
WLAN Pi R4. Version 1.0 is designed primarily for the WLAN Pi graphical
front-panel menu and its 128x128 display. A separate command-line interface is
available for terminal use, replay, diagnostics, and development.

The application analyzes QBSS information advertised in beacon frames. It does
not track observed clients, and it keeps AP-advertised QBSS utilization separate
from optional local radio survey utilization.

![WLANPi Beacon Live CU Screen](docs/images/utilization-cu-screen.png)

## Getting started: WLAN Pi front-panel app

This is the primary installation and usage path.

### Requirements

- WLAN Pi R4 (Raspberry Pi 4) running WLAN Pi OS.
- Waveshare 1.44-inch 128x128 LCD HAT with control stick, as supported by WLAN
  Pi FPMS.
- WLAN Pi FPMS 2.x installed and working. The installer expects `/usr/sbin/fpms`
  and exactly one FPMS Python package under `/opt/wlanpi-fpms/lib/python*/site-packages/fpms`.
- A monitor-mode-capable Wi-Fi interface named `wlan0`.
- Root access through `sudo`.
- Python 3.9 or newer with virtual-environment support.
- `git`, `ip`, `iw`, `tshark`, and `dumpcap`.
- An adapter, driver, and regulatory domain that allow the selected 20 MHz
  channel. Menu entries are not a guarantee that the local radio may tune every
  listed channel.

On WLAN Pi OS, many or all system packages may already be installed. Install
any missing packages with:

```bash
sudo apt update
sudo apt install python3 python3-venv git iproute2 iw tshark wireshark-common
```

`wireshark-common` supplies `dumpcap`. The FPMS installation supplies its own
Python runtime, Pillow support, display/GPIO integration, Scanner font, and the
`wlanpi-fpms` service; this repository does not replace those components.

Package and component summary:

| Package or component | Needed for | Purpose |
| --- | --- | --- |
| `wlanpi-fpms` 2.x | FPMS app | Existing WLAN Pi menu, LCD, GPIO, Pillow, and Scanner font runtime |
| `python3` 3.9+ | All workflows | Application and installer runtime |
| `python3-venv` | FPMS and CLI installs | Creates `/opt/wlanpi-beacon-live` or `.venv` |
| `git` | Source installation | Clones and updates this repository |
| `iproute2` | Live capture | Supplies `ip` to bring the interface down/up |
| `iw` | Live capture | Sets monitor mode/channel and optionally reads survey counters |
| `tshark`, `wireshark-common` | Live capture | Supplies TShark and Dumpcap packet-capture components |
| `python3-pip` | CLI/development only | Installs the project into `.venv`; not used by the FPMS installer |
| `setuptools>=64`, `wheel` | CLI/development only | Python build requirements resolved by `pip` |
| `pytest>=7.4,<9` | Tests only | Installed by the `dev` extra |

There are no third-party Python runtime dependencies in the application
environment created by this project.

### Install

Clone the repository and run the FPMS installer:

```bash
git clone https://github.com/Yanzzee/WLANPi-Utilization.git
cd WLANPi-Utilization
sudo ./scripts/install_wlanpi_fpms.sh
```

For an existing clone:

```bash
cd WLANPi-Utilization
git pull
sudo ./scripts/install_wlanpi_fpms.sh
```

The installer does not run `apt` or `pip`. It:

- creates an isolated application environment at `/opt/wlanpi-beacon-live`;
- copies the version 1.0 application into that environment and creates the
  `wlanpi-beacon-live` and `beacon-live` launchers;
- copies the thin display/menu adapter into the installed FPMS package;
- adds `Utilization` at the bottom of the FPMS Apps menu;
- adds the FPMS page-exit callback used by the left control-stick action;
- creates `/run/wlanpi-beacon-live` and `/var/log/wlanpi-beacon-live`;
- keeps one-time `.beacon-live.bak` copies of the two modified FPMS files; and
- restarts `wlanpi-fpms`.

The patch targets the current FPMS 2.x Python layout and exits with an error if
its required files or patch anchors are not found. Rerun the installer after a
`wlanpi-fpms` package upgrade because the upgrade can replace the adapter or
patched files.

### Start from the hardware menu

Use the control stick to select:

```text
Apps
  Utilization
    2.4 GHz | 5 GHz | 6 GHz PSC | 6 GHz All
      <channel and center frequency>
        Display | Display + Log
```

The four band menus contain the application's static, unbonded 20 MHz channel
lists. `6 GHz PSC` contains only preferred scanning channels. In `6 GHz All`,
PSC entries are prefixed `PSC`; other entries are prefixed `Ch`.

Selecting `Display` starts capture immediately without log files. Selecting
`Display + Log` starts the same display and writes both per-second CSV stats and
raw valid-beacon JSONL records under `/var/log/wlanpi-beacon-live`.

Press the control stick left while the display is open to stop capture and
return to the menu. FPMS sends SIGINT to the foreground application, which
stops TShark and flushes and closes enabled logs. Use up/down to move through
the Utilization, Admission, Stations, and Retries screens. Navigation changes
only the active screen; capture, analysis, and graph history continue.

### Exact FPMS launch commands

For `5 GHz > Ch 36 5180 MHz > Display`, FPMS runs:

```text
/opt/wlanpi-beacon-live/bin/wlanpi-beacon-live --iface wlan0 --band 5 --channel 36 --lcd-frame /run/wlanpi-beacon-live/display.ppm
```

For `5 GHz > Ch 36 5180 MHz > Display + Log`, FPMS runs:

```text
/opt/wlanpi-beacon-live/bin/wlanpi-beacon-live --iface wlan0 --band 5 --channel 36 --lcd-frame /run/wlanpi-beacon-live/display.ppm --stats-csv --beacons-jsonl --log-dir /var/log/wlanpi-beacon-live
```

Other menu selections use the same commands with the selected band and channel.
The band argument is `2.4`, `5`, or `6`. FPMS owns the physical display and
control-stick input; the child process only writes its PPM graph frame and JSON
display state under `/run/wlanpi-beacon-live`.

## Graphical display reference

The display is a 128x128-pixel frame with two header rows, a centered graph,
and two footer rows. It intentionally has no clock and no on-screen exit hint.
Log timestamps are unaffected.

```text
┌──────────────────────────────┐
│ frequency       STA n SUM n  │  header row 1
│ CU n% AVG n% MAX n%          │  header row 2
│ ┌──────────────────────────┐ │
│ │                          │ │
│ │  120-second QBSS CU      │ │  120x64 graph
│ │                          │ │
│ └──────────────────────────┘ │
│ SSID                   -RSSI │  footer row 1
│ BSSID                channel │  footer row 2
└──────────────────────────────┘
```

The diagram shows field placement, not exact character widths.

### Header row 1: radio and station counts

The first row displays frequency and the following fields:

- `STA`: the QBSS station count advertised by the BSSID selected as the current
  utilization source.
- `SUM`: the sum of the latest QBSS station counts advertised by every BSSID
  observed in that second. This channel-wide total is shown because stations
  across BSSIDs share airtime on the selected channel.

Station values from 0 through 999 are shown exactly. A larger value is shown as
`∞`; this is display formatting only. Stats CSV retains the uncapped `SUM`, and
beacon JSONL retains each raw advertised station count.

Band information is not shown. FPMS chooses the first frequency form that fits
horizontally:

1. frequency with `MHz`, `STA`, and `SUM`, such as
   `5180MHz STA 2 SUM 14`;
2. frequency without the `MHz` suffix, such as `5180 STA 999 SUM 999`, when
   three-digit counts and the installed font metrics require the shorter form.

If no frequency is available, the row contains only `STA` and `SUM`.

### Header row 2: utilization summary

The second row displays:

- `CU`: the current selected QBSS channel-utilization percentage;
- `AVG`: the arithmetic mean of valid selected CU values in the visible
  120-second window; and
- `MAX`: the maximum valid selected CU value in that window.

Percentages use whole-number display rounding. Missing values appear as `--`
and are excluded from `AVG` and `MAX`.

### Graph

- The plot is 120 pixels wide by 64 pixels high, from `x=4` through `x=123`
  and `y=30` through `y=93`.
- A dim border occupies the adjacent left and right columns, leaving four
  display pixels outside the graph on each side.
- Each horizontal pixel represents one completed one-second aggregation.
- The newest value is at the right edge. During startup, unused history is
  blank on the left; after 120 samples, older values scroll off the left.
- The single green bar series is selected AP QBSS channel utilization. Station
  counts are text only and are not graphed in version 1.0.
- Raw QBSS CU is an integer from 0 through 255 and is converted to percentage as
  `raw / 255 * 100`. The graph maps it into 64 four-value buckets; values 0-3
  occupy the bottom pixel and 252-255 reach the top.
- Dim horizontal guides occur at the top, bottom, and each 16-pixel quarter.
- A second without a selected QBSS CU value is a gap rather than a zero and is
  excluded from the summary.
- The first live publish cycle is a silent warm-up and is not added to the graph
  or stats CSV. Valid raw beacons from that cycle are still eligible for JSONL
  logging.

On the Retries screen, each graph column is an independent one-second sample:
received frames with the Retry bit set divided by retry-eligible frames received
in that second. The denominator includes retry-capable unicast management and
data frames. Beacons, probe requests, Action No Ack, group-addressed traffic,
control/extension frames, and frames without a readable Retry bit are excluded.
Every received retry transmission is counted, including multiple retries of the
same original frame. A nonzero value below 1% is displayed as `<1%` rather than
being rounded to `0%`.

The channel `RET` value is not scoped to the footer BSSID. It uses every decoded
retry-eligible frame heard in monitor mode on the tuned channel, including
traffic not addressed to the WLAN Pi. Live buckets are finalized only after an
ordered capture timestamp enters the next second, so a wall-clock redraw cannot
publish a partial bucket while TShark still has older rows buffered.

The Retries footer shows the BSSID with the highest retry percentage for the
current second. A tie keeps the previously displayed retry BSSID. If no retries
occurred, the footer shows the strongest beacon BSSID by RSSI. Frames can be
associated with that BSSID through the BSSID, transmitter, receiver, source, or
destination MAC-address fields.

### Footer

The first footer row left-aligns the selected SSID and right-aligns its strongest
RSSI for that second. RSSI has no label. SSID is the only field that may be
truncated, and an ellipsis indicates truncation.

The second footer row left-aligns the selected BSSID and right-aligns the
configured channel. Neither field has a label. If no QBSS-bearing beacon was
selected, the SSID is `<No QBSS Beacons>`. `<HIDDEN>` is reserved for a selected
beacon whose SSID is actually empty or hidden.

### Selection, colors, and text

CU and admission-capacity selection considers QBSS-bearing beacons using the
shared RSSI hysteresis and station-count rules. The newest beacon in the rolling
window is authoritative for the selected BSSID. Station totals are computed
independently from the latest beacons of all observed BSSIDs. Retry analysis uses
the same capture pipeline but also consumes eligible non-beacon frames.

The graph has a black background, dim blue-gray guides/borders, and a green CU
series. Header/footer text is white except for the yellow CU summary row. All
text uses FPMS Scanner's DejaVu Sans Mono Bold face at 10 pixels. The renderer
uses measured 3- or 4-pixel gaps between header fields instead of full
monospaced space cells; field alignment is unchanged. A 9- then 8-pixel safety
fallback remains for unexpected installed font metrics.

Horizontal space is the constraint. At expected 10-pixel Scanner metrics, the
worst-case frequency/`STA`/`SUM` row uses all 126 available pixels, the
`CU`/`AVG`/`MAX` row uses about 122 of 124 pixels, and a full BSSID beside a
three-digit channel uses all 124 footer pixels. Vertically, 10-pixel text fits
with the first header at `y=3`; the other text rows and the 120x64 graph retain
their positions.

## Secondary use: command line

The CLI is a separate workflow. It does not use the FPMS menu, control stick, or
physical display. Use it for terminal live capture, replay, logging, local
survey diagnostics, and development.

### CLI requirements and installation

Install the system packages, create a project virtual environment, and install
the package:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git iproute2 iw tshark wireshark-common
git clone https://github.com/Yanzzee/WLANPi-Utilization.git
cd WLANPi-Utilization
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The Python package has no third-party runtime dependencies. `setuptools` and
`wheel` are build requirements. Development and test installation adds only
`pytest>=7.4,<9`:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

### Live terminal capture

Live mode configures the interface for monitor mode, tunes a 20 MHz channel,
starts TShark, and updates once per second. It requires root privileges:

```bash
sudo .venv/bin/beacon-live live --iface wlan0 --channel 36
```

Use the explicit virtual-environment path under `sudo`; the activated shell
`PATH` is commonly not preserved. Stop with Ctrl-C. The foreground launcher
used internally by FPMS can also be run directly:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --channel 36
```

For an explicit frequency or an unambiguous 6 GHz channel mapping:

```bash
sudo .venv/bin/beacon-live live --iface wlan0 --frequency-mhz 5975
sudo .venv/bin/beacon-live live --iface wlan0 --band 6 --channel 5
```

Do not pass a frequency to `--channel`; for example, `--channel 5975` requests
channel number 5975 rather than frequency 5975 MHz.

Enable one or both live log formats with flags. Live flags select formats, not
filenames:

```bash
sudo .venv/bin/beacon-live live \
  --iface wlan0 \
  --channel 36 \
  --stats-csv \
  --beacons-jsonl \
  --log-dir logs
```

The application creates the log directory recursively. Both files share a
local-time run prefix containing a UTC offset, for example
`beacon_live_20260710T123045123456-0600_stats.csv` and
`beacon_live_20260710T123045123456-0600_beacons.jsonl`. Records contain local
ISO-8601 timestamps with offsets. CSV is flushed per stats row and JSONL per
beacon record.

Stats CSV columns are local time, interface, channel, resolved frequency/band,
unique BSSID count, uncapped QBSS station-count sum, selected QBSS CU,
SSID/BSSID/RSSI, and optional local survey CU. Beacon JSONL stores every valid
parsed beacon with the same run metadata plus SSID, BSSID, raw and percentage
QBSS CU, advertised station count, admission capacity, and RSSI.

Local survey channel utilization is an optional, driver-dependent metric. It is
not used by the graphical FPMS display. Add `--local-cu` to poll `iw dev
<iface> survey dump`, or `--survey-debug` to enable it and print counter
selection/deltas to stderr:

```bash
sudo .venv/bin/beacon-live live --iface wlan0 --channel 36 --local-cu
sudo .venv/bin/beacon-live live --iface wlan0 --channel 36 --survey-debug
```

Unsupported or unusable survey counters produce a warning while QBSS beacon
capture continues. AP QBSS CU and local survey CU are different measurements
and are never combined.

### Retry PCAP audit

To verify the retry numerator, denominator, and footer selection independently
of the LCD, stop the live display and collect a short monitor-mode capture on
the same channel. For channel 36, the exact setup and dump commands are:

```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set channel 36 HT20
sudo dumpcap -i wlan0 -a duration:60 -s 0 \
  -w /tmp/wlanpi-retry-debug.pcapng
```

`dumpcap` records all frames delivered by the monitor interface; there is no
destination-MAC capture filter. Use `iw dev wlan0 set freq <MHz> HT20` instead
of the channel command when testing an explicit center frequency.

Run the audit on the WLAN Pi or copy the PCAPNG to a development machine with
TShark and this package installed:

```bash
.venv/bin/beacon-live retry-debug \
  --input /tmp/wlanpi-retry-debug.pcapng \
  --output-csv /tmp/wlanpi-retry-audit.csv
```

Each `channel` CSV row is the exact one-second value used by `RET` and the
graph. It reports all decoded frames, frames with a readable Retry bit, mutually
exclusive group-address/missing-bit/non-retryable-type exclusions, the eligible
denominator, retry numerator, and percentage. The following `bssid` rows show
the same calculation for each known BSSID. `footer_bssid` and
`is_footer_bssid` identify the BSSID selected for the bottom two display lines.
PCAP files and audit output contain real network MAC addresses and SSIDs and
should be handled as sensitive diagnostic data.

### Replay without Wi-Fi hardware

Replay parses saved TShark TSV rows and requires neither root nor Wi-Fi
hardware:

```bash
.venv/bin/beacon-live replay --input samples/tshark_qbss_sample.tsv
```

Input fields are:

```text
frame.time_epoch, wlan.ssid, wlan.bssid, wlan.qbss.cu, wlan.qbss.scount, wlan.qbss.adc, radiotap.dbm_antsignal
```

Legacy six-field files without RSSI remain supported. Replay can also combine a
beacon TSV with saved before/after survey dumps:

```bash
.venv/bin/beacon-live replay \
  --beacons-tsv samples/pi_tshark_qbss_sample.tsv \
  --survey-before samples/pi_survey_before.txt \
  --survey-after samples/pi_survey_after.txt
```

See [Raspberry Pi / WLAN Pi testing](docs/pi_testing.md) for smoke capture,
debug collection, replay logging, physical acceptance checks, and troubleshooting.

## Version 1.0 scope

Version 1.0 focuses on beacon/QBSS channel-utilization analysis and the WLAN Pi
front-panel graph. It includes:

- live foreground capture and hardware-free TSV replay;
- strongest-BSSID QBSS selection once per second;
- selected-BSSID and summed QBSS station counts;
- a scrolling 120-second CU graph and four-row text layout;
- optional CSV stats and JSONL beacon logging;
- clean Ctrl-C and FPMS left-stick shutdown; and
- optional terminal-only local survey CU diagnostics.

Version 1.0 does not include observed-client tracking, station-count graphing,
Parquet output, selectable graph duration, background logging-only operation,
an information page, or debug options in the hardware menu.
