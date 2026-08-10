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

## Validate bonded capture and retry metrics

Use one application-owned PCAPNG for both live analysis and the independent
comparison. This avoids a second capture process competing for the adapter:

```bash
sudo .venv/bin/wlanpi-beacon-live \
  --iface wlan0 --band 5 --channel 36 \
  --log --log-dir /tmp/beacon-live-validation \
  --raw-pcapng /tmp/beacon-live-validation/channel36.pcapng
```

Let it run for at least 60 seconds, then stop it normally. Keep all of:

- `channel36.pcapng`, the full-length packets written by the exact TShark
  process that supplied live fields;
- `channel36.pcapng.json`, containing requested/actual definitions, read-back
  status, coverage warnings, negotiated TShark fields, record counts, and
  reported drops when available; and
- the application stats CSV and beacon JSONL.

Do not add a MAC filter or snap length. Snapshot truncation occurs after RF
demodulation and cannot make a 20 MHz receiver decode an 80 MHz transmission.
A global short snap length can also truncate variable Radiotap/MAC headers or
the beacon IEs required by existing metrics, so live diagnostics use `-s 0`.

Run the application audit, explicitly naming the selected primary frequency:

```bash
.venv/bin/beacon-live retry-debug \
  --input /tmp/beacon-live-validation/channel36.pcapng \
  --target-frequency-mhz 5180 \
  --output-csv /tmp/beacon-live-validation/retry-seconds.csv \
  --frames-csv /tmp/beacon-live-validation/retry-frames.csv
```

The per-second audit reports out-of-primary-scope exclusions separately. The
packet CSV adds PHY bandwidth, captured/original lengths, FCS status, Retry,
sequence, fragment, QoS TID, A-MPDU reference, and BSSID/TA/RA/SA/DA wherever
the driver and installed TShark expose them.

### Independent Wireshark/TShark comparison

The packet CSV and application audit share application parsing, so they are
diagnostics—not independent proof. Open the raw PCAPNG in Wireshark and add
columns for `wlan.fc.retry`, `wlan.seq`, `wlan.frag`, `wlan.qos.tid`,
`wlan.bssid`, `wlan.ta`, `wlan.ra`, `wlan.sa`, `wlan.da`, `frame.cap_len`,
`frame.len`, `wlan.fcs.status`, and the Radiotap VHT/HE/EHT bandwidth fields
available in that installation.

For each target BSSID and one-second interval:

1. Build the denominator from unicast data and retry-capable unicast management
   MPDUs with a readable Retry bit. Exclude beacons, probe requests, Action No
   Ack, control/extension frames, group RA/DA, and unreadable Retry bits.
2. Count the same eligible rows with Retry set for the numerator. Count repeated
   sequence/fragment/TID tuples separately; each captured retry transmission is
   an MPDU observation.
3. Confirm BSSID association through any of BSSID/TA/RA/SA/DA, and confirm its
   latest preceding beacon advertises primary 5180 MHz.
4. Compare those independent counts to `retry-seconds.csv` and the live stats
   CSV. Investigate timestamp-boundary, malformed, FCS, and drop diagnostics
   before attributing a difference to the ratio calculation.

You can export independent fields without the application parser for a separate
spreadsheet/pivot-table count:

```bash
tshark -n -r /tmp/beacon-live-validation/channel36.pcapng \
  -Y wlan -T fields -E header=y -E separator=, -E quote=d \
  -E occurrence=a \
  -e frame.time_epoch -e wlan.fc.type -e wlan.fc.subtype \
  -e wlan.fc.retry -e wlan.bssid -e wlan.ta -e wlan.ra \
  -e wlan.sa -e wlan.da -e wlan.seq -e wlan.frag -e wlan.qos.tid \
  > /tmp/beacon-live-validation/independent-tshark.csv
```

### Controlled width matrix

Repeat the same-primary test with controlled AP/client traffic at 20, 40+, 40-,
80, and 160 MHz where the adapter supports each mode. For every run:

1. Keep primary/control channel 36 (5180 MHz); change only advertised width and
   center. Generate sustained unicast traffic in both directions and introduce
   controlled attenuation/interference so nonzero retries occur.
2. Confirm the beacon JSONL definition and `iw dev wlan0 info` actual width and
   centers. The sidecar must say `complete`; otherwise treat the run as partial.
3. Confirm Radiotap shows frames using the wider bandwidth and that application
   retry counts include them.
4. Add a second AP whose own primary is channel 40 inside the captured 80 MHz
   block. Confirm its eligible traffic appears in the raw PCAPNG but is counted
   under `out_of_primary_scope_excluded_count`, not the channel-36 ratio.
5. Exercise mixed BSSIDs on primary 36 advertising narrower widths. Confirm one
   widest compatible definition covers them without channel hopping.
6. If available, repeat 80+80, HE 6 GHz, and EHT 320/punctured cases. Unsupported
   or unrepresentable modes must warn and report partial/HT20 coverage rather
   than claim complete capture.

Passive monitor capture cannot obtain literally every transmitted frame. The
validation target is every valid frame that this configured radio, PHY, driver,
regulatory state, and TShark can successfully decode.

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
`/run/wlanpi-beacon-live` and `/var/log/wlanpi-beacon-live`, installs the two
commands in `/usr/local/bin`, and restarts FPMS. Rerun it after an FPMS package
upgrade.

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
sudo wlanpi-beacon-live \
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
sudo wlanpi-beacon-live --iface wlan0 --channel 36
```

Explicit 6 GHz frequency:

```bash
sudo wlanpi-beacon-live --iface wlan0 --frequency-mhz 5975
```

Band-qualified 6 GHz channel:

```bash
sudo wlanpi-beacon-live --iface wlan0 --band 6 --channel 5
```

Optional local survey diagnostics:

```bash
sudo wlanpi-beacon-live \
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
