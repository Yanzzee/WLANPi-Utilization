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
  `/opt/wlanpi-beacon-live`; the installer now supplies the pinned Core client
  there, independently of Core's package-managed environment.
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
with the final device bundle. The user reported testing the item 8 changes on
October 13, 2026; fresh-image/reboot/reinstall coverage was not detailed in that
report. The same report confirms Core 2.4.8-1, successful capture-WebSocket
authentication, a populated tri-band wlanpi0 frequency list, and no sessions on
wlanpi-bd2. An opt-in Core-owner backend was subsequently implemented.

Subsequent Core-owner runs passed on 2.4 GHz channel 1 / 20 MHz (899 decoded
rows normalized to 899 frames) and 5 GHz channel 40 / 20 MHz (518 rows normalized
to 518 frames), with zero malformed rows, no transport error, confirmed Core
stop and verified radio restoration. On October 14, the user reported successful
direct and Core installs, a good 6 GHz CLI display and successful FPMS starts on
each band. Exact widths and backend-by-band combinations were not supplied.
Broader width/lifecycle/failure coverage remains pending; these results do not
prove complete frame delivery at every hardware configuration.

**Backend decision accepted October 14, 2026:** direct capture remains the
recommended default for the standalone CLI and FPMS application. Core stays
optional for remote integration and future compatible sharing. Successful Core
tests do not schedule a default change. Complete the direct-backend acceptance
below before publishing; broader optional Core work is tracked separately.

On October 14, 2026, the user reported sufficient device testing and confidence
to merge the current version into `main`. This records merge acceptance; it does
not claim that every individual test case or hardware combination above was
exercised. The final local regression run passed 624 tests with one skip.
Core expansion and the high-utilization performance observation remain
documented follow-ups. Tagging/publishing a release remains a separate step.

## Final device acceptance

1. On a clean target device with FPMS installed, use the [normal installation](../README.md#install-on-wlan-pi).
   Record the repository revision and OS/package versions in a diagnostic bundle.
   Confirm both CLI launchers, hardware-free replay, and `scripts/check_tshark.py`.
   For an existing installation previously opted into Core, select
   `sudo ./scripts/install_wlanpi_fpms.sh --fpms-backend direct` for these
   direct-backend release checks and verify no service environment override
   selects Core. Reinstallation without a backend flag preserves the saved choice.
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

- **SSID text:** normalization is implemented and readable names were confirmed
  on the device. Valid UTF-8 names are shared by displays/logs; hidden names retain their hidden
  label, and non-text/control bytes use `hex:…`. Verify known names in FPMS,
  CLI and logs after reinstallation, including hidden/Unicode/non-text cases
  that were not detailed in the latest device report.
- **Core capture backend:** direct remains the CLI and initial FPMS default.
  Installed Core capture now has its own client dependency and an opt-in FPMS
  setting (`--fpms-backend core`); basic installed-path checks were reported
  successful on October 14, with broader acceptance pending. Further
  Core expansion is a separate optional follow-up, with no default migration
  scheduled. Its current backend is owner-only and still needs local radio
  safety/restoration; the guard is not a system-wide broker used by all tools.
  See [the backend decision and optional acceptance](core-capture-evaluation.md).
- **High-utilization display performance:** the October 14 device report notes
  some direct-capture display lag while Core/API display appeared unaffected.
  Retain this as a performance follow-up for a controlled comparison; no measured
  drop-rate difference or backend-default change is established. See
  [the observation and comparison scope](core-capture-evaluation.md#high-utilization-performance-observation).
- **Driver coverage:** an iw-verified width does not prove complete frame
  delivery. Treat unsupported widths, missing survey counters, and incomplete
  frame delivery explicitly; see [capture validation](pi_testing.md#validate-bonded-capture-and-retry-metrics).
