# Troubleshooting

Start with the [README](../README.md) for supported hardware and installation.
This guide covers common WLAN Pi, capture, display, metric, and logging issues.

## Installer cannot find FPMS

The front-panel installer expects:

- `/usr/sbin/fpms`;
- one FPMS Python package under
  `/opt/wlanpi-fpms/lib/python*/site-packages/fpms`; and
- the FPMS 2.x files/patch anchors used by the bundled adapter installer.

Confirm FPMS is installed and running:

```bash
ls -l /usr/sbin/fpms
systemctl status wlanpi-fpms --no-pager
```

An FPMS package upgrade can replace the app adapter or patched navigation
files. Rerun the installer afterward:

```bash
sudo ./scripts/install_wlanpi_fpms.sh
```

The patcher keeps one-time `.beacon-live.bak` copies of modified FPMS files.

## A required command is missing

Run the setup check:

```bash
./scripts/pi_setup.sh
```

On Raspberry Pi OS/Debian, the usual packages are:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git iproute2 iw tshark wireshark-common
```

`wireshark-common` supplies `dumpcap`. The project has no third-party Python
runtime dependency; the `dev` extra adds pytest.

## `sudo: beacon-live: command not found`

`sudo` may not preserve the activated virtual environment's `PATH`. Call the
venv launcher explicitly:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --channel 36
```

or:

```bash
sudo .venv/bin/python -m beacon_live.cli live --iface wlan0 --channel 36
```

## The Wi-Fi interface is missing

List interfaces known to `iw`:

```bash
iw dev
```

The FPMS integration currently uses `wlan0`. CLI and test scripts can use
another name through `--iface` or the script's first argument.

Confirm the adapter/driver supports monitor mode:

```bash
iw phy
```

## The channel cannot be set

Possible causes include adapter capability, driver limitations, regulatory
domain, DFS restrictions, or ambiguous channel numbers.

Inspect available frequencies and regulatory state:

```bash
iw phy
iw reg get
```

Use an explicit center frequency when testing 5 GHz or 6 GHz:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --frequency-mhz 5975
```

For band-qualified channel notation, 6 GHz channel 5 maps to 5975 MHz:

```bash
sudo .venv/bin/wlanpi-beacon-live --iface wlan0 --band 6 --channel 5
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

## The interface remains in monitor mode

Live capture configures the selected interface for monitor mode. The current
foreground runtime does not automatically restore managed mode on every exit.
If this adapter should return to normal client operation, restore it manually:

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

Use the smoke capture and inspect its beacon TSV:

```bash
./scripts/pi_smoke.sh wlan0 5180
sed -n '1,20p' samples/pi_tshark_qbss_sample.tsv
```

Empty QBSS columns indicate the observed beacons did not provide those fields.
Try a channel containing an AP known to advertise QBSS load.

## Local survey utilization is unavailable or always zero

Local survey CU is optional and driver-dependent. It is different from an
AP's advertised QBSS utilization.

Enable diagnostics:

```bash
sudo .venv/bin/wlanpi-beacon-live \
  --iface wlan0 --channel 36 --survey-debug
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

See [screens.md](screens.md#3-composition) and
[architecture.md](architecture.md#radio-grouping).

## Retry data is blank or unexpected

A blank retry sample means no eligible frame with a readable Retry bit was
captured in that second. Beacons, group-addressed frames, control frames, and
several management subtypes are intentionally excluded.

Use the offline retry audit to inspect numerator, denominator, and exclusion
reasons:

```bash
.venv/bin/beacon-live retry-debug \
  --input /path/to/capture.pcapng \
  --output-csv /tmp/retry-audit.csv
```

See [screens.md](screens.md#5-retries) and the detailed capture procedure in
[pi_testing.md](pi_testing.md#capture-and-audit-retry-metrics).

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

## Collect a debug bundle

```bash
./scripts/pi_collect_debug.sh
```

The script creates `debug/pi_debug_<timestamp>/` and a matching `.tar.gz`
archive. It collects system, Wi-Fi, TShark, interface, rfkill, and recent kernel
information when available.

Review before sharing: bundles and captures can contain real SSIDs, BSSIDs,
MAC addresses, interface addresses, kernel messages, and other local details.
