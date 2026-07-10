# Raspberry Pi / WLAN Pi Smoke Testing

These scripts help collect first-pass evidence from a Raspberry Pi or WLAN Pi
without implementing the full live capture app yet. They do not install or
modify system services, and they do not integrate with the WLAN Pi display or
menu system.

## Clone the Repo

On the Pi:

```bash
git clone https://github.com/<your-org-or-user>/WLANPi-Utilization.git
cd WLANPi-Utilization
```

If you already cloned the repo, update it instead:

```bash
cd WLANPi-Utilization
git pull
```

## Create or Activate a Virtual Environment

Using a project-local virtual environment is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

If you already have a virtual environment for this repo, activate that one
before running setup. `scripts/pi_setup.sh` uses the active virtual environment
when `VIRTUAL_ENV` is set; otherwise it falls back to `python3 -m pip`.

The project supports Python 3.9 or newer.

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
  --channel 5975
```

The log directory is created automatically. Stats CSV rows are flushed after
each per-second record, and beacon JSONL is flushed line by line. Log rows
include interface, channel, 20 MHz width, and replay start-time metadata.

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
