# Trixie migration and release checks

This migration targets WLAN Pi OS 26.10 DeadEye / Debian 13, Python 3.13,
TShark 4.4, and the FPMS 1.4.24 menu layout. Utilization v1.0.0 Bookworm remains
the earlier release; this branch does not preserve Bookworm compatibility.

## Changes

- FPMS installation discovers the active package through `/usr/sbin/fpms` and
  its interpreter, validates patches, and restores the previous FPMS files if
  installation or the service restart fails.
- Key 2 rotates the live display through FPMS. FPMS remaps navigation and
  Keys 1/3 to match orientation; capture and the shared history continue.
- Automatic interface discovery supports the root and named network namespaces,
  prefers an existing suitable monitor, and reports ambiguous adapter choices.
- The direct capture backend temporarily owns the selected physical radio,
  checks competing use, and restores saved interface state and known monitor
  tuning on normal exit and handled startup failures.
- Python setup requires an isolated 3.13+ environment. Production remains under
  `/opt/wlanpi-beacon-live`; no third-party Python runtime packages are needed.
- TShark field checks run before radio preparation. Optional metadata is
  negotiated. Width and survey parsing reject misleading declarations and
  invalid counter pairs.
- Diagnostics now collect OS/package versions, effective FPMS paths and
  interface overrides, deployment fingerprints, service/journal evidence,
  TShark field types, and interfaces/controllers/sockets in each named namespace.

One capture and shared analysis pipeline still supplies every display and log.
The two-minute history continues across screen changes and rotation. AP QBSS
and local survey utilization remain separate, as do advertised station counts
and observed client counts.

## Evidence so far

The user has accepted iterative device testing of roadmap items 1–7. The latest
report confirms successful production reinstallation on Python 3.13 and the
read-only packet check on the Pi's TShark 4.4.19: all 52 requested fields are
available, and the HT40, VHT160, HE80, and EHT320 fixtures pass.

Those original synthetic packets verify decoding of RSSI, QBSS, operation
widths, puncturing, and repeated retries. They do not establish live driver
coverage at every width/band or usable survey counters on every adapter.
Record the exact image/package versions and tested band/width combinations
with the final device bundle. No fresh-image or final reboot/reinstall result
for item 8 has been supplied yet.

## Final device acceptance

1. On a clean target device with FPMS installed, use the [normal installation](../README.md#install-on-wlan-pi).
   Record the repository revision and OS/package versions in a diagnostic bundle.
   Confirm both CLI launchers, hardware-free replay, and `scripts/check_tshark.py`.
2. Open **Apps → Utilization → band → channel → Display**. Verify live data,
   all six screens, rotation with correctly remapped buttons, and normal exit.
   Exercise **Display + Log**, then **Start Logging / Stop Logging**. Confirm
   one capture instance and radio restoration; follow the detailed
   [Pi acceptance checks](pi_testing.md).
3. Reboot normally. Verify FPMS starts, the Utilization menu remains present,
   Display works, and radio state is restored after exit. Repeat installation
   twice and confirm a single menu entry and a healthy service.
4. Run the expanded collector after a test session:

   ```bash
   sudo ./scripts/pi_collect_debug.sh --since "15 minutes ago"
   ```

   Check `SUMMARY.txt` and `manifest.json`. Inspect version, FPMS path,
   deployment, and namespace reports. Missing optional commands can be recorded
   as unavailable; investigate failed essential checks or checkout mismatches.
5. Test a reversible startup failure without taking over another tool's radio:
   select an explicit nonexistent interface in the CLI, then collect its error
   alongside the bundle:

   ```bash
   sudo wlanpi-beacon-live --iface utilization-missing --band 2.4 --channel 1 \
     > /tmp/utilization-startup-failure.txt 2>&1
   sudo ./scripts/pi_collect_debug.sh --since "5 minutes ago"
   ```

   The CLI should fail with a useful target error and change no radio. CLI
   stderr is not part of the FPMS journal; retain that text file separately.
   For any naturally occurring FPMS failure, collect promptly and verify the
   bundle's journal identifies the cause without rerunning capture.
6. Record untested hardware combinations and resolve release-blocking failures
   before tagging or publishing the Trixie release. This document does not
   mark the release published.

## Known follow-ups

- **SSID text:** TShark 4.4 emits `wlan.ssid` as hex bytes. Display normalization
  remains a separate task, including hidden and non-text SSIDs.
- **Core capture backend:** this release uses direct TShark capture with its own
  per-radio guard. Core's capture WebSocket integration remains a separate
  architectural option under [active evaluation](core-capture-evaluation.md);
  the guard is not a system-wide broker used by all tools.
- **Driver coverage:** an iw-verified width does not prove complete frame
  delivery. Treat unsupported widths, missing survey counters, and incomplete
  frame delivery explicitly; see [capture validation](pi_testing.md#validate-bonded-capture-and-retry-metrics).
