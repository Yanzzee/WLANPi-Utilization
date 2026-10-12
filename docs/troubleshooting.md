# Troubleshooting

Start with the [README](../README.md) for supported hardware and installation.
This guide covers common WLAN Pi, capture, display, metric, and logging issues.

## Installer cannot find FPMS

The front-panel installer expects:

- `/usr/sbin/fpms`;
- a direct absolute Python shebang in that launcher, pointing to an executable
  FPMS interpreter with one installed `fpms` package; and
- the FPMS 1.4.24 layout tested with WLAN Pi OS 26.10 DeadEye.

Confirm FPMS is installed and running:

```bash
ls -l /usr/sbin/fpms
head -n 1 /usr/sbin/fpms
systemctl status wlanpi-fpms --no-pager
```

The installer follows the launcher symlink and asks its Python interpreter for
the package location without importing FPMS or initializing hardware. It prints
`Resolved FPMS Python package: ...` before patching. Stale Python directories
under `/opt/wlanpi-fpms/lib` and packages supplied through `PYTHONPATH` do not
affect discovery. The venv interpreter path is preserved even if its executable
is a symlink to the system Python.

Missing interpreters, invalid package locations, shell wrappers, and `/usr/bin/env`
shebangs produce an explicit discovery error. Unsupported package layouts are
rejected before FPMS files are modified. For a custom launcher, the patcher
supports an explicit `--fpms-package` path; the normal installer uses discovery.

An FPMS package upgrade can replace the app adapter or patched navigation
files, or change its Python environment. Rerun the installer afterward:

```bash
sudo ./scripts/install_wlanpi_fpms.sh
```

The patcher keeps one-time `.beacon-live.bak` copies of modified FPMS files.
It validates all proposed files before writing. If writing or the FPMS restart
fails, it restores the files as they were immediately before that installation
attempt, including any existing adapter and backups. A failed restart also
triggers a restart and check of the restored FPMS installation. The application
environment and CLI launchers remain installed.

If the installer reports `FPMS page-exit branch not found` on Trixie, update to
the installer that matches Python syntax rather than single-quoted text, then
rerun the full installer. It can repair the partial `fpms.py` patch left by the
earlier installer. An installation error produces a nonzero exit status; a
recovery error is reported separately. Inspect the service and journal before
retrying:

```bash
systemctl status wlanpi-fpms --no-pager
sudo journalctl -u wlanpi-fpms -n 100 --no-pager
```

## `sudo: unable to resolve host`

This warning means the device hostname is not represented correctly in
`/etc/hosts`. It is separate from the Beacon Live installer and does not cause
an FPMS patch-anchor failure.

Compare the configured hostname with the local host entry:

```bash
hostname
cat /etc/hostname
grep -v '^#' /etc/hosts
```

On a typical WLAN Pi, `/etc/hosts` should contain a line using the exact name
reported by `hostname`, for example:

```text
127.0.1.1 wlanpi-2g
```

Correct the hostname or `/etc/hosts` entry using the normal WLAN Pi OS system
configuration. The warning can coexist with a successful command, but fixing
it avoids repeated `sudo` delays and noise.

## A required command is missing

Run the setup check:

```bash
python3 -m venv .venv
source .venv/bin/activate
./scripts/pi_setup.sh
```

On Raspberry Pi OS/Debian, the usual packages are:

```bash
sudo apt update
sudo apt install python3 python3-venv git iproute2 iw tshark wireshark-common
```

`wireshark-common` supplies `dumpcap`. The project has no third-party Python
runtime dependency; the `dev` extra adds pytest.

## Development setup requires a Python 3.13 venv

`scripts/pi_setup.sh` installs development tools only into a genuine activated
venv. If it reports no venv or Python below 3.13, create and activate a fresh
environment with the Trixie interpreter. Setting `VIRTUAL_ENV` manually is not
sufficient: its interpreter must actually belong to that environment. Venvs
created with `--system-site-packages` are rejected.

Use `python3 --version` to check the system interpreter and
`python -c 'import sys; print(sys.version, sys.prefix, sys.base_prefix)'` after
activation. If ensurepip cannot repair missing pip, install `python3-venv` and
create a fresh environment. Packaging/editable-install failures stop with pip's
error output; check that output and connectivity before retrying. The script
does not install packages into system Python or bypass Debian's packaging
protections. Production installation uses the separate `/opt` venv.

## `sudo: beacon-live: command not found`

The normal FPMS installer creates both commands in `/usr/local/bin`. Rerun it
after an application or FPMS upgrade, then inspect the launchers and their
isolated targets:

```bash
sudo ./scripts/install_wlanpi_fpms.sh
command -v wlanpi-beacon-live beacon-live
ls -l /usr/local/bin/wlanpi-beacon-live /usr/local/bin/beacon-live
ls -l /opt/wlanpi-beacon-live/bin/wlanpi-beacon-live \
  /opt/wlanpi-beacon-live/bin/beacon-live
```

If a custom `sudo` configuration omits `/usr/local/bin` from its secure PATH,
use the absolute system launcher:

```bash
sudo /usr/local/bin/wlanpi-beacon-live --band 5 --channel 36
```

The `.venv/bin` commands are separate and are intended only for repository
development.

## The Wi-Fi interface is missing

List interfaces known to `iw`:

```bash
iw dev
```

The app and live CLI default to `--iface auto`. They inspect the root namespace
and named namespaces from `ip netns list`, then consider managed or monitor
interfaces whose phy supports monitor mode and the selected frequency. Existing
monitor interfaces are preferred. If several candidates remain, capture stops
with their names and asks for an explicit choice. An interface named `wlan0`
may belong to a radio without monitor support; interface numbering is not a
capability check.

Use `--iface capture1` to select an interface in the root namespace. For a named
namespace, also pass `--netns capture-lab`. Using `--netns capture-lab` with
automatic discovery restricts the search to that namespace. Capabilities,
tuning, channel verification, TShark, and survey reads all use that context.
For example:

```bash
ip netns list
sudo ip netns exec capture-lab iw dev
sudo wlanpi-beacon-live --iface capture1 --netns capture-lab --band 2.4 --channel 1
```

To configure another front-panel interface, run `sudo systemctl edit wlanpi-fpms`
and add the following, replacing `capture1` with the chosen interface:

```ini
[Service]
Environment="WLANPI_UTILIZATION_IFACE=capture1"
# Include this only when capture1 is in a named namespace:
Environment="WLANPI_UTILIZATION_NETNS=capture-lab"
```

Then run `sudo systemctl restart wlanpi-fpms`. The adapter reads this setting
when FPMS starts. Leave both overrides unset for automatic discovery across
namespaces, or set only `WLANPI_UTILIZATION_NETNS` to search one namespace.
Remove an obsolete override when a radio is renamed or moved. The capture
journal reports the discovered target as `root:wlanpi0` or, for example,
`capture-lab:capture1`.

Confirm the adapter/driver supports monitor mode:

```bash
iw phy
```

If selecting Display leaves the menu visible and the journal reports
`iw dev wlan0 set type monitor` followed by `Operation not supported`, capture
has failed to start on the selected radio. Update and rerun the installer to
use automatic discovery and the failed-launch menu recovery. The updated
adapter briefly reports an exited capture child and returns to the channel
menu. Inspect `journalctl -u wlanpi-fpms` for the underlying command error.

## The channel cannot be set

Possible causes include adapter capability, driver limitations, regulatory
domain, DFS restrictions, or ambiguous channel numbers.

Inspect available frequencies and regulatory state:

```bash
iw phy
iw reg get
```

For bonded capture, also inspect the actual definition:

```bash
iw dev wlanpi0 info  # replace with the selected capture interface
```

The default is fixed at 20 MHz on 2.4 GHz and 80 MHz on 5 GHz and 6 GHz. The
application does not retune in response to beacon operation fields. If the
default 80 MHz definition is unsupported or rejected, it warns and retries once
at 20 MHz. An explicit override undergoes the same capability check and is not
silently changed:

```bash
sudo wlanpi-beacon-live --iface auto --frequency-mhz 5180 \
  --channel-width 80 --center-frequency1-mhz 5210
```

The automatic 80 MHz center is the standard block containing the selected
primary. For explicit non-default definitions, check the AP's HT/VHT/HE/EHT
operation element and the regulatory/driver capability together.

For 6 GHz PSC primaries that can belong to a full 80 MHz channel (the PSC
sequence 5, 21, …, 213), automatic capture selects that PSC's unique standard
80 MHz block. PSC 229 is at the upper band edge and has no complete standard
80 MHz block inside 5925–7125 MHz, so automatic capture falls back to 20 MHz
there.

`Fixed capture has partial coverage` should appear only when a complete beacon
operation definition is not contained by the actual fixed capture—for example,
an AP advertises 160 MHz while the adapter is capturing only 80 MHz, or the
advertised center conflicts with the configured block. Missing operation
fields alone are not proof of partial coverage. When only the per-frame radio
frequency is available, it is used to confirm the primary scope without
showing this warning. Interactive CLI mode renders any confirmed warning in the
managed footer so it cannot disrupt the curses layout.

Normal live capture uses a 1024-byte snapshot length. A warning naming the BSSID
and original length means a larger beacon was truncated and late information
elements may be unavailable. Interactive CLI mode renders this warning in its
managed footer so it does not corrupt the screen. Re-run with `--raw-pcapng`
when a full-length diagnostic capture is needed.

## Display remains responsive but a graph has empty recent points

Live graphs are fixed to the preceding 120 wall-clock seconds. If capture
aggregation cannot deliver a completed second before its display slot, that
slot is intentionally shown as unavailable; old data is not scrolled forward
and presented as current. Check the TShark and `beacon-live-aggregate`
processes separately:

```bash
pgrep -af 'wlanpi-beacon-live|beacon-live-aggregate|tshark'
free -m
vmstat 1
```

Normal live mode uses a dedicated aggregation process and does not retain
individual data frames. Sustained swap activity or a saturated TShark process
therefore points to system, driver, or dissection pressure rather than the GUI
waiting to replay a 120-second frame-object queue.

Use an explicit primary frequency when testing 5 GHz or 6 GHz:

```bash
sudo wlanpi-beacon-live --iface auto --frequency-mhz 5975
```

For band-qualified channel notation, 6 GHz channel 5 maps to 5975 MHz:

```bash
sudo wlanpi-beacon-live --iface auto --band 6 --channel 5
```

Do not pass a frequency to `--channel`; `--channel 5975` means channel number
5975 and will normally fail.

## Capture stops or TShark fails

Check installed versions and permissions:

```bash
tshark --version
dumpcap --version
sudo tshark -D
```

Inspect service output for FPMS-launched sessions:

```bash
sudo journalctl -u wlanpi-fpms -n 100 --no-pager
```

Look for leftover processes only after the app should have stopped:

```bash
pgrep -af 'wlanpi-beacon-live|tshark'
```

## The radio is busy

Utilization checks the whole phy, including interfaces other than the selected
capture interface. A capture on `wlan1` can conflict with `wlanpi0` when both
belong to the same phy. Packet sockets identify captures from TShark/dumpcap,
tcpdump, Scapy/Profiler, Kismet, and Core capture workers. AP/other interface
modes, connected managed interfaces, and network controllers also prevent
borrowing the radio. Core's background service and an idle `wlanpi0` alone do
not count as active use.

The packet-socket check distinguishes `lldpd` link discovery on managed
interfaces from capture ownership. Those `lldpd` sockets are allowed; other
processes and unidentified sockets remain blockers. The exception does not
apply to monitor-interface or namespace-wide sockets. If an older installation
reports `Radio ... is busy: lldpd (pid ...)`, update this checkout and rerun
`sudo ./scripts/install_wlanpi_fpms.sh`. Leave `lldpd` running. This corrects the
item 5 false positive caused by treating every bound packet socket as a capture.

Stop or release the radio through the owning tool, then retry. Another adapter
can be selected explicitly using `--iface` and optionally `--netns`. The app
does not kill tools, stop Core, or disconnect a Wi-Fi connection automatically.
If the only radio carries your management connection, use another management
path before releasing it for monitor capture.

NetworkManager-managed interfaces are refused even while disconnected; iwd in
the same namespace is conservatively refused because it may scan or reconnect.
Release those interfaces through the network controller before capture. Missing
permissions or unreadable ownership/state information produce an error rather
than bypassing the checks.

If the journal reports `nmcli ... device show wlanpi0 failed: Error: Device
'wlanpi0' not found`, update this checkout and rerun
`sudo ./scripts/install_wlanpi_fpms.sh`. This was an item 5 startup regression:
the guard queried every iw interface through NetworkManager even when NM had no
device object for an existing monitor. The corrected guard first lists NM's
devices, checks ownership only for listed radio interfaces, and uses a fixed
locale for machine-readable values. It still rejects managed interfaces and
failed inventory/ownership queries. Do not add the monitor to NetworkManager
or stop NetworkManager as a workaround.

The lock under `/run/wlanpi-beacon-live/radios/phyN.lock` prevents two
Utilization instances from using the same radio. The file can remain after exit;
the kernel-held lock, not file existence, indicates ownership. Do not remove a
live lock file. Other applications do not share this lock. Keep other tools
off that radio during capture; preflight cannot prevent a later external retune.

## The interface remains in monitor mode

Live capture now restores the saved interface mode, administrative state, and
known monitor tuning after normal exit, Ctrl+C, SIGTERM, or startup failure.
An interface that started in monitor mode, such as Core's `wlanpi0`, remains a
monitor after exit. This is its prior state, not a cleanup failure. Existing
interfaces and namespace placement are preserved.

Inspect the journal for `Radio restoration incomplete` if state was not
restored. An unplugged/replaced interface is not modified. SIGKILL, power loss,
or driver failure can bypass or prevent cleanup. There is no automatic crash
recovery journal in this version; inspect the current radio and restore it
through the owning tool. If an idle adapter should return to normal client
operation, the manual commands are:

```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type managed
sudo ip link set wlan0 up
```

The smoke-test script records the original interface type and attempts to
restore it when that script exits.

## No QBSS values appear

Not every AP advertises a QBSS Load element. Beacon capture can be working even
when Utilization shows `<No QBSS Beacons>` or graph gaps.

Exit Utilization in FPMS, then save one application-owned capture:

```bash
sudo wlanpi-beacon-live --iface auto --band 5 --channel 36 \
  --raw-pcapng /tmp/utilization-qbss.pcapng
```

After stopping with Ctrl-C, inspect the saved beacons without reopening a radio:

```bash
tshark -r /tmp/utilization-qbss.pcapng -Y 'wlan.fc.type_subtype == 0x08' \
  -T fields -e wlan.bssid -e wlan.qbss.cu -e wlan.qbss.scount
```

Empty QBSS columns indicate the observed beacons did not provide those fields.
Try a channel containing an AP known to advertise QBSS load.

## Local survey utilization is unavailable or always zero

Local survey CU is optional and driver-dependent. It is different from an
AP's advertised QBSS utilization.

Enable diagnostics:

```bash
sudo wlanpi-beacon-live \
  --iface auto --band 5 --channel 36 --local-cu --survey-debug
```

Diagnostic lines show target frequency, active/busy deltas, computed value,
and an explanation. If active time increases while busy time stays zero, the
driver is reporting an idle/unsupported busy counter. If target-frequency
counters are unavailable, the local value cannot be trusted for that channel.
Beacon capture and the normal screens can continue.

## Composition grouping looks wrong

Estimated radios depend on vendor OUI, AP-name fields, RSSI similarity, and
related BSSID MAC patterns. AP-name fields are vendor-specific and may be
absent. The Composition screen intentionally favors conservative grouping, so
it can overcount physical radios rather than merge uncertain BSSIDs.

See [screens.md](screens.md#5-composition) and
[architecture.md](architecture.md#radio-grouping).

## Retry data is blank or unexpected

A blank retry sample means no eligible frame with a readable Retry bit was
captured in that second. Beacons, group-addressed frames, control frames, and
several management subtypes are intentionally excluded.

`wlan.fc.retry.expert` in Wireshark is an expert label generated from the same
Frame Control Retry bit as `wlan.fc.retry`; it is not a separate retry source.
The live parser also reads the complete `wlan.fc` value as a fallback. For older
TShark versions that do not expose the 6 GHz HE Operation primary-channel
field, the capture's per-frame radio frequency provides the primary-scope
fallback instead of excluding every frame from the denominator.

Also inspect the data subtypes in the raw capture. A tested MT7921U could tune
an 80 MHz 6 GHz definition and capture Null/QoS Null frames while omitting
payload-bearing QoS Data seen by another capture device; the same adapter did
capture payload-bearing data in 5 GHz. If direct Dumpcap has the same result,
this is below the application capture path and should be treated as a possible
adapter/driver/firmware limitation. Tuning success alone does not establish
complete frame delivery.

Use the offline retry audit to inspect numerator, denominator, and exclusion
reasons:

```bash
beacon-live retry-debug \
  --input /path/to/capture.pcapng \
  --output-csv /tmp/retry-audit.csv
```

See [screens.md](screens.md#4-retries) and the detailed capture procedure in
[pi_testing.md](pi_testing.md#validate-bonded-capture-and-retry-metrics).

## Beacon received count briefly shows zero

`BCN_REC` is not a count of every beacon on the channel. It is the number of
phase-matched beacons received for recently heard BSSIDs above -75 dBm during
that completed capture second. Other screens can still show RSSI, QBSS, or
composition values from the latest beacon retained in the rolling window.

A zero for one second means no beacon was assigned to an expected slot for the
qualifying BSSIDs in that second. Beacon Loss eligibility lasts for that
reporting second plus the 102.4 ms delayed-beacon allowance; after that, an
inactive high-RSSI BSSID cannot keep the metric at zero. If a zero persists,
use the beacon JSONL timestamps and raw PCAPNG to distinguish a capture gap
from missing decoded beacon rows.

## Logs are missing

- `Display` intentionally does not start logging.
- Use `Display + Log` for a graph and logs.
- Use `Start Logging` for a logging-only background session.
- FPMS files are under `/var/log/wlanpi-beacon-live`.
- CLI files are under `--log-dir` (default `logs`).

Check permissions and free space:

```bash
sudo ls -la /var/log/wlanpi-beacon-live
df -h /var/log/wlanpi-beacon-live
```

Logging stops by default below 256 MiB free. Inspect the last CSV/JSONL record
for an `END_OF_LOG` disk-pressure marker. See [logging.md](logging.md).

## Display does not update

Confirm FPMS is running and the app artifacts exist:

```bash
systemctl status wlanpi-fpms --no-pager
sudo ls -la /run/wlanpi-beacon-live
sudo journalctl -u wlanpi-fpms -n 100 --no-pager
```

Rerun the installer after FPMS or repository updates. If terminal live capture
works but the LCD does not, the issue is likely in FPMS integration, display
hardware, or the installed adapter rather than parsing/analysis.

## TShark fields or hex SSID names

Run the read-only packet check from the updated checkout:

```bash
/opt/wlanpi-beacon-live/bin/python scripts/check_tshark.py
```

Field inspection now stops before radio discovery/preparation if TShark cannot
be queried, times out, or lacks a required field. The journal or CLI error
names the problem. Missing optional AP metadata is omitted safely.

TShark 4.4.19 reports `wlan.ssid` as FT_BYTES and emits names as hex. This is a
confirmed separate issue awaiting parser normalization; changing adapters or
reinstalling FPMS does not fix it. Keep a known SSID/capture for that follow-up.

## Collect a debug bundle

Run from the checkout after reproducing a failure:

```bash
sudo ./scripts/pi_collect_debug.sh --since "15 minutes ago"
```

Use `--output-dir /tmp/utilization-debug` to choose another destination or
`--timeout-seconds 20` if a slow device needs more time per command. The script
creates a unique `debug/pi_debug_<timestamp>_<suffix>/` directory and matching
`.tar.gz`. Root allows collection of journals, runtime files, and named network
namespaces. Without it, permission failures are recorded; the script does not
prompt for sudo during collection.

Start with these files:

| Evidence | Purpose |
| --- | --- |
| `SUMMARY.txt`, `manifest.json` | Show each failed, unavailable, timed-out, or mismatched check; the command files retain exit status and stderr. |
| `os_release.txt`, `wlanpi_release.txt`, `packages.txt` | Identify the actual image, Debian, FPMS, Core, Python, and wireless-tool versions. |
| `fpms_service.txt`, `fpms_journal.txt` | Identify service state/exit status, unit paths, and recent capture/installation errors. |
| `fpms_paths.json`, `fpms_python_probe.txt`, `fpms_environment.txt`, `fpms_process_environment.json` | Resolve the launcher's interpreter/package, configured unit overrides, and actual running-process interface/namespace overrides without importing FPMS hardware code. |
| `app_python.txt`, `deployment.json`, `cli_launcher_*.txt` | Identify the production venv and stale app/adapter files compared with the current checkout. |
| `tshark_version.txt`, `tshark_fields.json` | Identify TShark version, requested field types, and unavailable fields. |
| `namespaces.json`, `iw_dev.txt`, `netns_N_*` | Map root/named contexts to their interface, phy, regulatory, controller, and packet-socket evidence. |
| `processes.txt`, `radio_locks.txt`, `runtime_*`, `disk.txt`, `memory.txt` | Identify active tools, app artifacts, disk pressure, and memory pressure. |

For `Capture Stopped (2)`, read `fpms_journal.txt` first. Use the named target
in that error to select the matching namespace files. A busy-radio error can
be compared with `packet_sockets.txt`, `packet_owners.txt`, and process IDs
in the matching namespace; a stale-code suspicion
can be checked through `matches_checkout` in `deployment.json`. Command failures
do not discard the rest of the bundle. A successful collection means the
archive was written, not that every diagnostic check succeeded.

The collector opens no live capture, tunes no radio, acquires no radio lock,
and restarts/stops no service. It does not copy log CSVs, raw packet captures,
or screenshots. Retain those separately when comparing metrics. CLI errors
must also be saved separately; they do not appear in the FPMS journal.

Review before sharing: bundles and captures can contain SSIDs, BSSIDs, MAC/IP
addresses, process arguments, kernel messages, and other local details. Only
Utilization's interface/namespace overrides are retained from service environment
output; unrelated environment values are excluded.
