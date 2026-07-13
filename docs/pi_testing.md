# WLANPi Beacon Live 1.0: Raspberry Pi / WLAN Pi Testing

These instructions cover hardware-free replay, Pi smoke capture, terminal live
testing, and WLAN Pi R4 front-panel installation. The smoke/setup scripts do
not change services; the separate `install_wlanpi_fpms.sh` installer performs
the documented FPMS integration and service restart.

For the normal, primary WLAN Pi FPMS installation, use:

```bash
git clone https://github.com/Yanzzee/WLANPi-Utilization.git
cd WLANPi-Utilization
sudo ./scripts/install_wlanpi_fpms.sh
```

The complete hardware, OS, package, installation, menu, and graphical-display
requirements are in the [README](../README.md). The virtual-environment and
`pi_setup.sh` steps below are for CLI use and development verification; they are
not prerequisites for the FPMS installer, which creates its own isolated
environment under `/opt/wlanpi-beacon-live`.

## Clone the Repo

On the Pi:

```bash
git clone https://github.com/Yanzzee/WLANPi-Utilization.git
cd WLANPi-Utilization
```

If you already cloned the repo, update it instead:

```bash
cd WLANPi-Utilization
git pull
```

## Create or Activate a Virtual Environment

For CLI testing and development, use a project-local virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

If you already have a virtual environment for this repo, activate that one
before running setup. `scripts/pi_setup.sh` uses the active virtual environment
when `VIRTUAL_ENV` is set; otherwise it falls back to `python3 -m pip`.

The project supports Python 3.9 or newer.

On Raspberry Pi OS or Debian, install the complete CLI/development system
package set with:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git iproute2 iw tshark wireshark-common
```

The project itself has no third-party Python runtime dependencies. The
development extra installs `pytest>=7.4,<9`; `setuptools` and `wheel` are build
requirements. On a WLAN Pi, FPMS separately supplies Pillow, the Scanner font,
LCD/GPIO support, and its service environment.

## Run Setup Checks

```bash
./scripts/pi_setup.sh
```

The setup script checks for:

- `python3`
- `pip`
- `iw`
- `tshark`
- `dumpcap`
- `git`

If any commands are missing, it prints example `apt` commands for you to run
manually. It does not install packages automatically.

When dependencies are present, it installs this repo in editable mode and runs:

```bash
pytest
```

## Run the Smoke Capture

The smoke script accepts an interface plus either a channel number or a
frequency in MHz:

```bash
./scripts/pi_smoke.sh [iface] [channel-or-frequency-mhz]
```

Defaults:

```bash
./scripts/pi_smoke.sh wlan0 36
```

The script assumes a 20 MHz channel width and configures the selected interface
for monitor-mode capture on `HT20`.

The three Wi-Fi bands should be supported when the adapter, driver, and local
regulatory domain allow them:

```bash
./scripts/pi_smoke.sh wlan0 2412  # 2.4 GHz, channel 1
./scripts/pi_smoke.sh wlan0 5180  # 5 GHz, channel 36
./scripts/pi_smoke.sh wlan0 5975  # 6 GHz, channel 5 PSC
```

Frequency in MHz is preferred for 5 GHz and 6 GHz testing because channel
numbers can be ambiguous across bands. For example, 5 GHz channel 149 is
`5745 MHz`, while 6 GHz channel 149 is `6695 MHz`.

For 6 GHz discovery testing, prefer PSC channels. 6 GHz channel 5 is a PSC and
uses `5975 MHz`; 6 GHz channel 1 uses `5955 MHz` and is not a PSC.

Channel numbers still work for convenience:

```bash
./scripts/pi_smoke.sh wlan0 6
```

To use a different adapter, pass that interface explicitly:

```bash
./scripts/pi_smoke.sh wlan1 5180
```

The script writes:

- `samples/pi_iw_phy.txt`
- `samples/pi_tshark_qbss_sample.tsv`
- `samples/pi_tshark_mac_sample.tsv`
- `samples/pi_survey_before.txt`
- `samples/pi_survey_after.txt`

It is okay if `pi_tshark_qbss_sample.tsv` contains no QBSS channel-utilization
values. Some APs do not advertise QBSS load elements, and the file is still
useful for parser and replay testing.

## Collect Debug Output

For troubleshooting, collect a timestamped debug bundle:

```bash
./scripts/pi_collect_debug.sh
```

The script creates `debug/pi_debug_<timestamp>/`, archives it as
`debug/pi_debug_<timestamp>.tar.gz`, and prints the archive path.

## Copy Files Back to the Mac

From your Mac, use `scp` with the Pi hostname or IP address:

```bash
scp pi@<pi-hostname-or-ip>:~/WLANPi-Utilization/samples/pi_*.txt samples/
scp pi@<pi-hostname-or-ip>:~/WLANPi-Utilization/samples/pi_*.tsv samples/
scp pi@<pi-hostname-or-ip>:~/WLANPi-Utilization/debug/pi_debug_*.tar.gz debug/
```

Adjust the remote path if you cloned the repo somewhere else on the Pi.

## Replay Copied Smoke Files

After copying the smoke-test files back to your Mac, replay them locally:

```bash
beacon-live replay \
  --beacons-tsv samples/pi_tshark_qbss_sample.tsv \
  --survey-before samples/pi_survey_before.txt \
  --survey-after samples/pi_survey_after.txt
```

The replay output includes per-second AP/QBSS beacon stats plus one local survey
CU value computed from the before/after survey dumps. The summary at the end
reports row counts, skipped malformed rows, timestamp range, duration, unique
BSSID count, and local survey CU when available.

The older replay option still works for simple beacon TSV files:

```bash
beacon-live replay --input samples/tshark_qbss_sample.tsv
```

To save replay output for later review, add stats CSV and raw beacon JSONL logs:

```bash
beacon-live replay \
  --beacons-tsv samples/pi_tshark_qbss_sample.tsv \
  --survey-before samples/pi_survey_before.txt \
  --survey-after samples/pi_survey_after.txt \
  --stats-csv logs/stats.csv \
  --beacons-jsonl logs/beacons.jsonl \
  --interface wlan0 \
  --channel 5
```

The log directory is created automatically. Stats CSV rows are flushed after
each per-second record, and beacon JSONL is flushed line by line. Log rows
put local record time first, followed by interface, channel, and resolved
frequency/band metadata.

## Install the WLAN Pi R4 Front-Panel App

The WLAN Pi FPMS process owns the Waveshare SPI display and control-stick GPIO.
Install this project as an FPMS app instead of trying to send terminal output to
the display:

```bash
sudo ./scripts/install_wlanpi_fpms.sh
```

The installer creates `/opt/wlanpi-beacon-live`, installs a thin FPMS adapter,
patches the FPMS Apps menu and page-exit callback, and restarts
`wlanpi-fpms`. The adapter launches/stops the child, copies graph frames, and
lays out already-formatted fields with FPMS's Scanner font. All beacon analysis,
summaries, graph rendering, and field formatting stay in this project. Rerun
the installer after upgrading the `wlanpi-fpms` package.

Navigate with the control stick:

```text
Apps
  Utilization
    2.4 GHz | 5 GHz | 6 GHz PSC | 6 GHz All
      <channel and center frequency>
        Display | Display + Log
```

PSC channels are marked in the `6 GHz All` list. Selecting `Display` or
`Display + Log` starts live beacon capture immediately. Press left while the
graph is open to send SIGINT to the capture process, terminate TShark, flush and
close any enabled logs, and return to the FPMS menu. No other control-stick or
button action is used by this first version.

`Utilization` appears after the existing entries at the bottom of Apps.

The exact command behind `5 GHz > Ch 36 5180 MHz > Display` is:

```text
/opt/wlanpi-beacon-live/bin/wlanpi-beacon-live --iface wlan0 --band 5 --channel 36 --lcd-frame /run/wlanpi-beacon-live/display.ppm
```

`Display + Log` uses the same command plus:

```text
--stats-csv --beacons-jsonl --log-dir /var/log/wlanpi-beacon-live
```

The 128x128 screen contains a centered 120x64 graph. It displays the latest 120
seconds from left to right with one pixel per second. One bar maps raw QBSS CU
`0-255` into 64 pixels. Once full, new seconds scroll in from the right. The
graph is shifted two pixels upward to increase separation from the footer.

Two larger rows above the graph show frequency, `STA`, `SUM`, selected `CU`,
rolling average `AVG`, and rolling maximum `MAX`. Band is included when it
fits; 2.4 GHz falls back from `2.4G` to `2G`, then to frequency plus `MHz`
without a band. `SUM` is the sum of the latest QBSS
station counts from every BSSID observed during that second, since those
stations share airtime on the channel. `STA` is the QBSS station count
advertised by the BSSID selected as the CU source. Values through 999 are exact;
larger values display as `∞`; stats CSV retains the uncapped sum and raw beacon
JSONL retains advertised station counts. Below the graph,
SSID and BSSID are left-aligned; unlabeled RSSI and channel are right-aligned on
their respective rows, matching the scanner layout. Long SSIDs are truncated.
The screen does not show a clock or exit hint; log timestamps are unchanged.
Missing CU values are graph gaps and do not enter the summary. When no QBSS
beacon is selected, SSID reads `<No QBSS Beacons>`.

Text uses Scanner's DejaVu Sans Mono Bold face at a stable 9 px, with an 8 px
safety fallback. The width constraints are the CU/AVG/MAX row at two-digit
utilization and the BSSID/channel footer; only SSID may truncate.

The AP selected each second is the QBSS-bearing BSSID with the strongest
observed RSSI; the latest beacon from that BSSID supplies CU and the individual
`STA` station count. The `SUM` station count is computed independently across
all observed BSSIDs. This is beacon-only analysis. It does not track observed
clients.

### On-Device Acceptance Check

After installation:

1. Open a Display channel and leave it open long enough to observe one new
   graph column each second.
2. Press left and confirm FPMS returns to the selected channel menu.
3. Open the same channel with Display + Log, wait several seconds, press left,
   and confirm both timestamped files exist:

   ```bash
   sudo ls -l /var/log/wlanpi-beacon-live
   ```

4. Confirm no capture child was left behind and inspect service errors:

   ```bash
   pgrep -af 'wlanpi-beacon-live|tshark'
   sudo journalctl -u wlanpi-fpms -n 100 --no-pager
   ```

The automated tests cover log local-time formatting, recursive frame/log
directory creation, one-second window updates, FPMS command construction,
SIGINT exit, and TShark cleanup. The checklist verifies the physical LCD, GPIO
control stick, radio, and installed OS packages together.

## Run Live Mode From a Terminal

For direct testing outside FPMS, run:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --channel 36
```

The terminal process stays in the foreground and exits cleanly on Ctrl-C.

When using the project virtual environment, call the venv Python explicitly
under `sudo`. This avoids the common `sudo: beacon-live: command not found`
problem caused by `sudo` using a different `PATH`.

```bash
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36
```

Use `--channel` for channel numbers. Use `--frequency-mhz` when you want to
tune by explicit center frequency:

```bash
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --frequency-mhz 5975
```

For 6 GHz channel notation, pass the band so the channel can be mapped to the
right frequency. 6 GHz channel 5 is a PSC and maps to `5975 MHz`:

```bash
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --band 6 --channel 5
```

Do not pass a frequency to `--channel`; `--channel 5975` asks `iw` for channel
number 5975 and the kernel will reject it as an unknown channel.

Defaults are:

```bash
sudo .venv/bin/wlanpi-beacon-live
```

Use Ctrl-C to stop. The TShark process is terminated on exit.

To keep per-second stats and valid raw beacon records from a live run, enable
one or both output types. You may select the containing directory, but not the
filenames:

```bash
sudo .venv/bin/python -m beacon_live.cli live \
  --iface wlan0 \
  --channel 36 \
  --stats-csv \
  --beacons-jsonl \
  --log-dir logs
```

The directory is created automatically. Both files use one app-generated local
timestamp and UTC-offset prefix, for example
`beacon_live_20260710T123045123456-0600_stats.csv` and
`beacon_live_20260710T123045123456-0600_beacons.jsonl`. The stats CSV is flushed
for each emitted second, and beacon JSONL is flushed line by line. Entries use
local ISO-8601 timestamps with offsets and include interface, channel, and
resolved frequency/band metadata. Stats rows include the selected QBSS CU,
source SSID/BSSID, and source beacon RSSI; beacon JSONL includes every valid
beacon and its RSSI. Fixed 20 MHz width and beacon-only record type fields are
omitted. Logging is disabled when neither output flag is present. Passing a
filename after either flag is rejected.

Local survey CU is driver-dependent and strictly opt-in. To poll
`iw dev <iface> survey dump` once per second and populate local CU, add
`--local-cu`:

```bash
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36 --local-cu
```

If local CU always shows `0.00%` or `unavailable`, run live mode with survey
diagnostics.
`--survey-debug` also enables local CU:

```bash
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36 --survey-debug
```

The diagnostic lines are printed to stderr and show the selected survey
frequency, active-time delta, busy-time delta, computed local CU, and reason.
If `busy_delta_ms=0` while `active_delta_ms` increases, the adapter/driver is
reporting an idle local busy counter. If the reason says the target frequency
counters are unavailable, the local survey data cannot be trusted for that tuned
channel. Without `--local-cu`, counters are not polled. The local CU dashboard
column is also omitted unless the flag is present. When enabled but the command
is unsupported, counters are missing, or output cannot be parsed, the column
displays `unavailable` and live mode prints a warning, then continues collecting,
displaying, and logging valid AP/QBSS data. This is an adapter/driver
survey-counter limitation, not a beacon-capture error.

If you installed the package system-wide, `sudo beacon-live live --iface wlan0
--channel 36` can also work, but the project-local venv form above is preferred
for WLAN Pi testing.

## What to Commit

Do not commit generated `samples/pi_*` files or `debug/pi_debug_*.tar.gz`
bundles by default. Do not commit generated `logs/` output by default either.
They can include real SSIDs, BSSIDs, client MAC addresses, interface addresses,
kernel logs, and local environment details.

The repo ignores those generated files. If a hardware capture is useful as a
regression fixture, create a small sanitized sample with a descriptive name that
does not start with `pi_`, then commit that curated fixture.

## Metric Notes

AP QBSS channel utilization and local survey channel utilization are different
metrics:

- AP QBSS CU is advertised by an access point in beacon frames. It describes
  that AP's view of channel utilization and is parsed from `wlan.qbss.cu`.
- Local survey CU is measured by the local adapter from `iw dev <iface> survey
  dump`. It is computed from counter deltas as `busy_delta / active_delta * 100`.

Keep these separate when comparing results. A Pi may hear an AP's QBSS report
while its local adapter measures a different busy percentage from its own
position, receive sensitivity, channel, and capture timing.
