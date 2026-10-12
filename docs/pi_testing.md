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

The project requires Python 3.13 or newer. Verify `python3 --version` reports
the Trixie interpreter before creating the venv. Typical Debian packages are:

```bash
sudo apt update
sudo apt install python3 python3-venv git iproute2 iw tshark wireshark-common
```

## Run setup checks

```bash
./scripts/pi_setup.sh
```

Run this without `sudo`, after activating the development venv. The script
rejects missing/fake venvs, Python below 3.13, and inherited system site-packages
before pip runs. It checks `ip`, `iw`, `tshark`, `dumpcap`, and `git`, upgrades
venv packaging tools, installs this repository in editable mode, checks both
CLI entrypoints and hardware-free replay, and runs pytest. It prints package
suggestions for missing commands but does not run `apt install`.

### Python 3.13 environment acceptance

1. In a terminal with no active venv, run `./scripts/pi_setup.sh`. It must stop
   with activation instructions before attempting package installation.
2. Create a fresh development environment using the Trixie interpreter:

   ```bash
   python3 --version
   python3 -m venv .venv-trixie-test
   source .venv-trixie-test/bin/activate
   ./scripts/pi_setup.sh
   python -c 'import sys; print(sys.version); print(sys.prefix); print(sys.base_prefix)'
   ```

   Confirm Python 3.13+, different prefix/base_prefix values, successful editable
   installation, CLI/replay checks, and pytest. Keep this test venv or deactivate
   it and return to your usual development venv afterward.
3. Reinstall production and verify it uses its separate environment:

   ```bash
   sudo ./scripts/install_wlanpi_fpms.sh
   /opt/wlanpi-beacon-live/bin/python -c 'import sys; print(sys.version); print(sys.prefix); print(sys.base_prefix)'
   wlanpi-beacon-live --help
   beacon-live replay --input samples/tshark_qbss_sample.tsv
   ```

   Confirm the production prefix is `/opt/wlanpi-beacon-live`, not the development
   venv or system Python. Exercise FPMS Display and left-button exit, then rerun
   the installer to check repeat installation. The installer restarts FPMS.
4. Automated checks also cover forged VIRTUAL_ENV, older interpreters,
   inherited system packages, install failures, production path boundaries,
   fresh production CLI/replay, and removal of stale app files on reinstallation.

## TShark 4.4 and metric acceptance

For roadmap item 7, first run this read-only check from the updated repository:

```bash
tshark --version
dpkg-query -W tshark wireshark-common iw
/opt/wlanpi-beacon-live/bin/python scripts/check_tshark.py
```

The check does not open an interface, change its state, or start a capture. It
reports the installed version and negotiated fields, then decodes four original
synthetic PCAPs with the production dissectors and shared analysis engine. Each
case must report `OK`: RSSI -47 dBm, QBSS CU 128/255, station count 12, admission
capacity 42, the expected HT/VHT/HE/EHT definition, and 3 retries / 4 eligible
frames (75%). An EHT320 fixture passing does not mean your adapter supports
320 MHz. Probe failures and missing required fields must now stop live startup
before radio preparation; missing optional metadata can be omitted safely.

Reinstall the application to test the updated live path:

```bash
sudo ./scripts/install_wlanpi_fpms.sh
```

Exercise FPMS Display and normal exit on 2.4 GHz channel 1, 5 GHz channel 36,
and an available 6 GHz channel such as 5. Test only bands your adapter supports.
Verify RSSI, QBSS, retry history, rotation/navigation, and radio restoration.
Some APs omit QBSS; that must remain unavailable rather than becoming zero.
Hex SSID names remain a separate known issue.

For metric comparison, exit Utilization in FPMS, then use the single-capture
workflow below with `--iface auto`. Collect one 60-second run per supported
band. For example, start with 2.4 GHz:

```bash
sudo /opt/wlanpi-beacon-live/bin/wlanpi-beacon-live \
  --iface auto --band 2.4 --channel 1 --channel-width 20 \
  --local-cu --survey-debug --log --log-dir /tmp/utilization-item7-24 \
  --raw-pcapng /tmp/utilization-item7-24/channel1.pcapng
```

Stop with Ctrl-C. Repeat with `--band 5 --channel 36` and `--band 6 --channel 5`,
using distinct log directories and filenames. Omit `--channel-width 20` to
exercise the default 80 MHz capture on 5/6 GHz, then test wider explicit
settings only where supported. Compare saved packets against logged QBSS/RSSI
and the independent retry counts described below. Check requested/actual
width and coverage in the PCAPNG sidecar; unsupported or unverified definitions
must remain explicit.

`--survey-debug` prints the selected local survey frequency and active/busy
counter deltas. Local CU must equal `100 * busy_delta / active_delta` for a
valid same-channel pair. Missing/reset counters or a busy delta greater than
active must produce unavailable CU. This is a separate measurement from AP
QBSS CU and need not agree with it. Retain output and record which band/width
combinations deliver frames and which expose usable survey counters.

## Legacy smoke capture

Prefer the application-owned capture workflow in [item 7](#tshark-44-and-metric-acceptance)
for Trixie validation. This older helper performs its own manual HT20 setup
and restores only the interface type; it does not use the application's radio
ownership guard or full state restoration. Use it only on an idle test adapter
after exiting Utilization and releasing the radio in other tools.

```bash
./scripts/pi_smoke.sh [iface] [channel-or-frequency-mhz]
```

Pass a known monitor-capable interface explicitly (replace `wlanpi0` as needed):

```bash
./scripts/pi_smoke.sh wlanpi0 36
```

The script sets the interface to monitor mode for a 15-second HT20 capture,
extracts normalized test inputs, reads survey counters, and attempts to restore
the original interface type when it exits.

Use explicit MHz for unambiguous 5/6 GHz testing:

```bash
./scripts/pi_smoke.sh wlanpi0 2412  # 2.4 GHz channel 1
./scripts/pi_smoke.sh wlanpi0 5180  # 5 GHz channel 36
./scripts/pi_smoke.sh wlanpi0 5975  # 6 GHz channel 5 PSC
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
  --iface auto --band 5 --channel 36 \
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

Do not add a MAC filter. Normal live analysis uses the application's 1024-byte
snapshot limit and reports any larger beacon it encounters. The `--raw-pcapng`
diagnostic workflow uses full-length `-s 0` capture so the saved evidence is not
truncated.

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
2. Confirm the beacon JSONL definition and `iw dev <selected-interface> info` actual width and
   centers. The sidecar must say `complete`; otherwise treat the run as partial.
3. Confirm Radiotap shows frames using the wider bandwidth and that application
   retry counts include them.
4. Add a second AP whose own primary is channel 40 inside the captured 80 MHz
   block. Confirm its eligible traffic appears in the raw PCAPNG but is counted
   under `out_of_primary_scope_excluded_count`, not the channel-36 ratio.
5. Exercise mixed BSSIDs on primary 36 advertising narrower widths. Confirm the
   fixed 80 MHz definition covers them without a retune or history reset.
6. Add a BSSID advertising bandwidth beyond or incompatible with the fixed
   80 MHz block. It must warn and report partial coverage without retuning or
   claiming complete capture. Test wider explicit CLI overrides only on an
   adapter known to support them.

Passive monitor capture cannot obtain literally every transmitted frame. The
validation target is every valid frame that this configured radio, PHY, driver,
regulatory state, and TShark can successfully decode.

An `iw`-verified width is not proof of complete frame delivery. WLAN Pi testing
with an MT7921U found that 80 MHz 6 GHz capture could contain Null/QoS Null
frames but omit payload-bearing QoS Data that a Mac capture device observed.
If a width/band combination shows this pattern in both the application-owned
PCAPNG and a direct Dumpcap capture, record it as an adapter/driver/firmware
limitation and do not interpret the resulting retry percentage as complete
channel coverage.

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

### Trixie menu installation acceptance

For roadmap item 1, use FPMS 1.4.24 on WLAN Pi OS 26.10 DeadEye. The installer
validates the proposed FPMS files before writing and checks that the restarted
service stays active for five seconds. Run it twice to check repeat installation:

```bash
dpkg-query -W wlanpi-fpms
python3 --version
sudo ./scripts/install_wlanpi_fpms.sh
sudo ./scripts/install_wlanpi_fpms.sh
systemctl is-active wlanpi-fpms
sudo journalctl -u wlanpi-fpms -n 100 --no-pager
```

Confirm Apps contains exactly one Utilization entry. Open Display on a channel
with known beacon activity, use up/down to change screens, and verify history
continues. Press left and confirm capture stops and the channel menu returns.
Record the OS, FPMS version, adapter, installer results, and button results in
[the roadmap](trixie-roadmap.md) before marking item 1 complete.

### FPMS package discovery acceptance

For roadmap item 2, inspect the launcher and rerun installation from the updated
checkout:

```bash
readlink -f /usr/sbin/fpms
head -n 1 /usr/sbin/fpms
dpkg-query -W wlanpi-fpms
sudo ./scripts/install_wlanpi_fpms.sh
systemctl is-active wlanpi-fpms
```

Confirm the printed `Resolved FPMS Python package` path belongs to the launcher's
Python environment. On DeadEye it normally ends in
`/opt/wlanpi-fpms/lib/python3.13/site-packages/fpms`. Open Apps → Utilization and
verify there is one menu entry and Display still works.

Automated tests cover stale Python directories and a simulated package update.
After a routine FPMS package update on the device, rerun the Utilization
installer and check the menu/display again; package updates can replace the
patched files. If discovery or layout validation fails, retain the exact error
and launcher/package versions for diagnosis. Record the device results in
[the roadmap](trixie-roadmap.md).

### Front-panel control acceptance

For roadmap item 3, rerun the installer from the updated checkout, open Display,
and allow history to accumulate. Record the capture/TShark PIDs with
`pgrep -af 'wlanpi-beacon-live|tshark'` before and after testing Key 2.

1. Confirm Key 1 does nothing during the display session.
2. Press Key 2. Confirm the display rotates 180 degrees immediately, stays on
   the same screen, and continues updating. Press it again to return to the
   previous orientation. Capture/TShark PIDs should stay the same.
3. Rotate again and use up/down to change screens. Confirm navigation remains
   usable and earlier graph history is retained. Check FPMS's flipped physical
   mapping: up/down, left/right, and Key 1/Key 3 swap; Key 2 and center stay the
   same. The physical Key 1 button saves screenshots when flipped, and physical
   Key 3 is disabled. The control in the screen-relative left direction exits.
4. Press Key 3. Confirm a timestamped PNG for the current screen is saved in
   `/var/log/wlanpi-beacon-live`, and its confirmation returns to the same screen.
5. Press left. Confirm capture stops and the built-in menu returns in the saved
   orientation. Verify Key 2 still rotates the built-in menu.
6. After exiting capture, run `sudo systemctl restart wlanpi-fpms` and confirm
   the orientation remains as set. Record the results in
   [the roadmap](trixie-roadmap.md).

The installed menu is:

```text
Apps
  Utilization
    2.4 GHz | 5 GHz | 6 GHz PSC | 6 GHz All
      <channel and center frequency>
        Display | Display + Log | Start Logging | Stop Logging
```

### Capture interface and namespace acceptance

After updating, reinstall the adapter and confirm discovery on the current Pi:

```bash
sudo ./scripts/install_wlanpi_fpms.sh
iw dev
iw phy
ip netns list
```

1. With interface/namespace service overrides unset, select Apps → Utilization
   → 2.4 GHz → channel 1 → Display. On the previously tested device, discovery
   should select `root:wlanpi0`, leaving the unsupported `wlan0` unselected.
   Check `sudo journalctl -u wlanpi-fpms -n 80 --no-pager` for
   `Capture interface: root:wlanpi0`. Confirm live data, navigation, rotation,
   and left-button exit still work.
2. Select Display + Log and confirm the stats CSV and beacon JSONL metadata
   identify the resolved interface. `network_namespace` is blank in CSV and
   null in JSON for root capture. Repeat with logging-only mode, then stop it.
3. After stopping the front-panel capture, test an explicit root interface:
   `sudo wlanpi-beacon-live --iface wlanpi0 --band 2.4 --channel 1 --local-cu`.
   Confirm capture and survey updates; stop with Ctrl+C.
4. If an available test radio already resides in a named namespace, inspect it
   with `sudo ip netns exec NAME iw dev` and
   `sudo ip netns exec NAME iw phy`. Run
   `sudo wlanpi-beacon-live --iface IFACE --netns NAME --band 2.4 --channel 1 --local-cu --log`.
   Replace NAME and IFACE with its actual identifiers. Verify capture, channel
   read-back, survey updates when supported, and namespace metadata. Repeat
   with `--netns NAME` alone to check discovery restricted to that namespace.
   The front-panel equivalent uses `WLANPI_UTILIZATION_NETNS=NAME` and optionally
   `WLANPI_UTILIZATION_IFACE=IFACE` in its service override.
5. When multiple equally suitable monitors exist, automatic discovery should
   report ambiguity and name the choices. Choose one explicitly and confirm it
   works. Renamed interfaces should work through discovery without a `wlan0`
   naming assumption. Automated tests cover these cases when hardware is absent.
6. Test a nonexistent namespace or interface with explicit CLI overrides and
   confirm a useful command/target error. A failed front-panel child should
   return to a responsive channel menu. Remove test overrides afterward.

Use an available test radio for namespace checks; do not move a radio used by
another tool to manufacture this test. Record any namespace/multiple-radio
checks that could not be exercised on the device.

### Radio ownership and restoration acceptance

Reinstall after updating the checkout:

```bash
sudo ./scripts/install_wlanpi_fpms.sh
iw dev
ip -j link show dev wlanpi0
iw dev wlanpi0 info
ip -j link show dev wlan1
```

Replace names with the actual capture interface and siblings on its phy. Save
this output as the baseline. The administrative `UP` flag matters; monitor
`operstate UNKNOWN` does not mean the interface is down. For a namespaced radio,
prefix each inspection command with `sudo ip netns exec NAME`.

For the NetworkManager startup regression, compare `iw dev` with
`LC_ALL=C nmcli -g DEVICE device status`. A monitor such as `wlanpi0` may be
present only in iw. That difference must not prevent startup. The corrected
guard queries NM ownership only for interfaces in NM's inventory; a listed
managed sibling must still prevent capture. Confirm the prior `Device 'wlanpi0'
not found` error no longer appears after reinstalling.

Keep `lldpd` running for these tests. Its link-discovery socket on an idle
managed interface must not produce a busy-radio error. A simultaneous tcpdump
or other capture still must, including one on that same managed interface.

1. Run Display on a different channel from the baseline, navigate/rotate, then
   exit left. Repeat the baseline commands. Confirm the capture interface's
   mode, UP flag, and original known channel/width are restored; siblings regain
   their prior UP flags. Existing monitor interfaces and namespaces must remain.
2. Repeat with Display + Log and logging-only Start/Stop. Start again to confirm
   the phy lock was released and the shared analysis/history still behaves as
   before.
3. With front-panel capture stopped, run
   `sudo wlanpi-beacon-live --iface wlanpi0 --band 2.4 --channel 6 --local-cu`.
   Stop with Ctrl+C and compare the baseline. Repeat and send SIGTERM to the
   application PID from a second terminal; compare again. Normal foreground
   capture handles SIGTERM through graceful cleanup.
4. While that CLI capture is running, attempt a second Utilization capture on
   the same interface (or another VIF of the same phy). Expect a busy/lock
   message and no changes to the first capture. Stop the first process normally.
5. To test an existing external capture, start
   `sudo tcpdump -i wlanpi0 -n -w /tmp/utilization-conflict.pcap` in a separate
   terminal on the idle test radio. Launch Utilization. Expect a busy message
   naming the capture process/socket and a responsive return to the channel
   menu. Verify no tuning/state change; stop tcpdump normally and retry.
6. When practical, repeat conflict checks with Profiler, a Kismet datasource,
   or a Core-managed connection on that same radio. Utilization must refuse
   without stopping the owner. Do not use a radio carrying the only management
   connection for this test. Confirm a tool on a separate phy is unaffected.
7. Using an available idle, uncontrolled managed interface, test explicit CLI
   capture and Ctrl+C. Confirm its original managed mode and UP/down flag return.
   When an available namespaced test radio exists, repeat restoration there.

Startup failure, partial preparation, restore-command failure, and replacement
hardware are covered by automated tests. Record any cases not exercised on the
device. External tools do not honor Utilization's lock: keep them stopped while
it captures. SIGKILL/power-loss recovery is not guaranteed or automatically
journaled; it is not the graceful-exit acceptance case.

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
7. While a display is active, confirm the first auxiliary button does nothing
   and the second rotates the display without restarting capture or resetting
   history. Press the third auxiliary button and confirm a timestamped PNG for
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
  --iface auto \
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
sudo wlanpi-beacon-live --iface auto --channel 36
```

Explicit 6 GHz frequency:

```bash
sudo wlanpi-beacon-live --iface auto --frequency-mhz 5975
```

Band-qualified 6 GHz channel:

```bash
sudo wlanpi-beacon-live --iface auto --band 6 --channel 5
```

Optional local survey diagnostics:

```bash
sudo wlanpi-beacon-live \
  --iface auto --band 5 --channel 36 --local-cu --survey-debug
```

The terminal process stays in the foreground and exits on Ctrl-C. Normal live
capture restores saved radio state; see
[the radio checks](#radio-ownership-and-restoration-acceptance) and
[troubleshooting.md](troubleshooting.md#the-interface-remains-in-monitor-mode).

## Collect a debug bundle

```bash
sudo ./scripts/pi_collect_debug.sh --since "15 minutes ago"
```

The script writes a unique `debug/pi_debug_<timestamp>_<suffix>/` directory and
matching `.tar.gz`. It collects bounded read-only checks and preserves failures
in `SUMMARY.txt` and `manifest.json`. See
[troubleshooting.md](troubleshooting.md#collect-a-debug-bundle) for the evidence
map and sharing guidance, and [the final Trixie release checks](trixie-release.md#final-device-acceptance)
for fresh installation, reboot, and repeat-install acceptance.

## Generated data

Do not commit raw `samples/pi_*`, `debug/pi_debug_*`, `logs/`, or PCAP files by
default. They may contain SSIDs, BSSIDs, MAC addresses, IP addresses, kernel
messages, and environment information. Commit only small sanitized fixtures
with accompanying tests.
