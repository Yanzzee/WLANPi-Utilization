# Raspberry Pi / WLAN Pi Testing

This is the hardware verification playbook for WLANPi Beacon Live. Normal users
should start with the [README](../README.md). Contributors should also read
[development.md](development.md) and [architecture.md](architecture.md).

The setup and smoke scripts do not change services. The separate FPMS installer
patches the Apps menu and restarts `wlanpi-fpms`.

## Prepare the repository

On the Pi:

```bash
git clone https://github.com/Yanzzee/WLANPi-Utilization.git
cd WLANPi-Utilization
```

For an existing clone:

```bash
cd WLANPi-Utilization
git pull
```

For CLI/development testing, create a project environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

The project requires Python 3.9 or newer. Typical Debian packages are:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git iproute2 iw tshark wireshark-common
```

## Run setup checks

```bash
./scripts/pi_setup.sh
```

The script checks `python3`, `pip`, `iw`, `tshark`, `dumpcap`, and `git`. When
they are available, it installs this repository into the active environment and
runs pytest. It prints package suggestions for missing commands but does not run
`apt install`.

## Run a smoke capture

```bash
./scripts/pi_smoke.sh [iface] [channel-or-frequency-mhz]
```

Default:

```bash
./scripts/pi_smoke.sh wlan0 36
```

The script sets the interface to monitor mode for a 15-second HT20 capture,
extracts normalized test inputs, reads survey counters, and attempts to restore
the original interface type when it exits.

Use explicit MHz for unambiguous 5/6 GHz testing:

```bash
./scripts/pi_smoke.sh wlan0 2412  # 2.4 GHz channel 1
./scripts/pi_smoke.sh wlan0 5180  # 5 GHz channel 36
./scripts/pi_smoke.sh wlan0 5975  # 6 GHz channel 5 PSC
```

Channel numbers can overlap bands. For example, channel 149 is 5745 MHz in
5 GHz and 6695 MHz in 6 GHz. Prefer a 6 GHz preferred scanning channel when
testing discovery.

Outputs:

- `samples/pi_iw_phy.txt`
- `samples/pi_tshark_qbss_sample.tsv`
- `samples/pi_tshark_mac_sample.tsv`
- `samples/pi_survey_before.txt`
- `samples/pi_survey_after.txt`

Empty QBSS fields do not necessarily indicate capture failure; some APs do not
advertise QBSS Load elements.

## Replay smoke data

On the Pi or after copying the files to another machine:

```bash
.venv/bin/beacon-live replay \
  --beacons-tsv samples/pi_tshark_qbss_sample.tsv \
  --survey-before samples/pi_survey_before.txt \
  --survey-after samples/pi_survey_after.txt
```

To verify replay output formats:

```bash
.venv/bin/beacon-live replay \
  --beacons-tsv samples/pi_tshark_qbss_sample.tsv \
  --survey-before samples/pi_survey_before.txt \
  --survey-after samples/pi_survey_after.txt \
  --stats-csv logs/replay-stats.csv \
  --beacons-jsonl logs/replay-beacons.jsonl \
  --interface wlan0 \
  --channel 36
```

## Capture and audit retry metrics

The retry analyzer uses all decoded monitor-mode WLAN frames on the tuned
channel, not only frames addressed to the WLAN Pi. Create a reproducible
60-second channel-36 capture:

```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set channel 36 HT20
sudo dumpcap -i wlan0 -a duration:60 -s 0 \
  -w /tmp/wlanpi-retry-debug.pcapng
```

For an explicit center frequency, replace the last `iw` command with, for
example:

```bash
sudo iw dev wlan0 set freq 5975 HT20
```

Do not add a MAC-address capture filter. Full frames are needed to audit retry
eligibility and BSSID association.

Run the audit:

```bash
.venv/bin/beacon-live retry-debug \
  --input /tmp/wlanpi-retry-debug.pcapng \
  --output-csv /tmp/wlanpi-retry-audit.csv
```

The audit emits one channel row and zero or more known-BSSID rows per capture
second. It includes decoded/readable counts, group-address/missing-bit/type
exclusions, eligible denominator, retry numerator/percentage, and footer
selection. A blank percentage means no eligible frames; `0.000000` means
eligible frames existed with no Retry bit set.

See [screens.md](screens.md#4-retries) for the metric rules.

## Copy artifacts to another machine

From a workstation:

```bash
scp pi@<pi-host>:~/WLANPi-Utilization/samples/pi_*.txt samples/
scp pi@<pi-host>:~/WLANPi-Utilization/samples/pi_*.tsv samples/
scp pi@<pi-host>:~/WLANPi-Utilization/debug/pi_debug_*.tar.gz debug/
scp pi@<pi-host>:/tmp/wlanpi-retry-debug.pcapng debug/
scp pi@<pi-host>:/tmp/wlanpi-retry-audit.csv debug/
```

Adjust paths for the actual clone location.

## Install the front-panel app

```bash
sudo ./scripts/install_wlanpi_fpms.sh
```

The installer creates `/opt/wlanpi-beacon-live`, installs the application and
thin FPMS adapter, patches the Apps menu/navigation callbacks, creates
`/run/wlanpi-beacon-live` and `/var/log/wlanpi-beacon-live`, and restarts FPMS.
Rerun it after an FPMS package upgrade.

The installed menu is:

```text
Apps
  Utilization
    2.4 GHz | 5 GHz | 6 GHz PSC | 6 GHz All
      <channel and center frequency>
        Display | Display + Log | Start Logging | Stop Logging
```

## On-device acceptance checklist

1. Select a channel with known AP beacon activity and open `Display`.
2. Confirm the Utilization graph gains samples as new capture seconds complete.
3. Use up/down to visit Utilization, Admission, Stations, Retries, Beacon Loss,
   and Composition.
4. On Stations, generate traffic from one or more associated clients. Confirm
   `MAC` increases only for newly seen client MACs in the two-minute window,
   and confirm cyan per-second bars overlay the amber advertised-count bars.
5. Return to earlier screens and confirm their two-minute histories continued
   while inactive.
6. Press left and confirm capture exits and the selected channel menu returns.
7. While a display is active, confirm the first two auxiliary buttons do
   nothing. Press the third auxiliary button and confirm a timestamped PNG for
   the active screen appears in `/var/log/wlanpi-beacon-live`. Confirm the
   brief saved-folder message clears back to the same screen.
8. Open `Display + Log`; confirm its brief start message includes the log
   folder. Wait several seconds, exit left, confirm the stop message, and
   confirm a CSV and JSONL file exist:

   ```bash
   sudo ls -l /var/log/wlanpi-beacon-live
   ```

9. Inspect the stats CSV and confirm `unique_client_mac_count` contains the
   unique eligible client count for each emitted second.
10. Use `Start Logging`; confirm no graph page opens. Then use `Stop Logging` and
   confirm both messages include the log folder, the child exits, and files are
   closed.
11. Confirm no unexpected capture child remains:

   ```bash
   pgrep -af 'wlanpi-beacon-live|tshark'
   ```

12. Inspect FPMS service output for errors:

   ```bash
   sudo journalctl -u wlanpi-fpms -n 100 --no-pager
   ```

### Disk-safety acceptance

Use a temporary CLI log directory and set the minimum above current free space
to force the safety path without filling a disk:

```bash
sudo .venv/bin/wlanpi-beacon-live \
  --iface wlan0 \
  --channel 36 \
  --logging-only \
  --log-dir /tmp/beacon-live-low-disk-test \
  --min-free-mb 999999999
```

Confirm the process exits cleanly and the final CSV/JSONL records contain the
low-disk marker described in [logging.md](logging.md#low-disk-markers).

## Terminal live checks

Basic channel capture:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --channel 36
```

Explicit 6 GHz frequency:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --frequency-mhz 5975
```

Band-qualified 6 GHz channel:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --band 6 --channel 5
```

Optional local survey diagnostics:

```bash
sudo .venv/bin/wlanpi-beacon-live \
  --iface wlan0 --channel 36 --survey-debug
```

The terminal process stays in the foreground and exits on Ctrl-C. Normal live
capture currently leaves the interface in monitor mode; see
[troubleshooting.md](troubleshooting.md#the-interface-remains-in-monitor-mode)
to restore managed mode.

## Collect a debug bundle

```bash
./scripts/pi_collect_debug.sh
```

The script writes `debug/pi_debug_<timestamp>/` and a matching `.tar.gz`
archive. See [troubleshooting.md](troubleshooting.md#collect-a-debug-bundle) for
privacy guidance.

## Generated data

Do not commit raw `samples/pi_*`, `debug/pi_debug_*`, `logs/`, or PCAP files by
default. They may contain SSIDs, BSSIDs, MAC addresses, IP addresses, kernel
messages, and environment information. Commit only small sanitized fixtures
with accompanying tests.
