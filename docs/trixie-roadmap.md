# WLAN Pi Trixie Compatibility Roadmap

Target WLAN Pi OS 26.10 DeadEye on Debian 13 Trixie, with Python 3.13,
TShark 4.4, and FPMS 1.4.24. Utilization v1.0.0 Bookworm remains the previous
release; future work does not need to preserve Bookworm compatibility.

Implement and test one item at a time, in the order below. Mark an item complete
after its automated checks and applicable on-device acceptance checks pass.
Record the tested OS, package versions, adapter, and result with each item.

## Milestones

- [x] **1. Repair FPMS menu installation.** Fix the confirmed quote-sensitive
  `buttons.py` patch failure. Validate all patches before writing, restore files
  if installation fails, and verify FPMS starts successfully after installation.
  **Acceptance:** regression tests cover the actual Trixie FPMS layout, repeat
  installation, and failure recovery; Apps → Utilization appears on the device,
  and up/down navigation and left-button exit work.
  **Status:** complete. Automated coverage uses the unmodified FPMS 1.4.24-1
  layout and simulated write/service failures. On October 12, 2026, the user
  confirmed the device works after changing the capture default to `wlanpi0`
  on `phy0` and adding failed-child menu recovery. The device test used
  2.4 GHz channel 1; `wlan0` on `phy1` does not support monitor mode.

- [ ] **2. Locate the active FPMS installation reliably.** Resolve the package
  through the interpreter used by the FPMS launcher instead of globbing every
  Python directory. Detect unsupported layouts and document reinstallation
  after FPMS package updates.
  **Acceptance:** stale Python directories do not affect discovery; an FPMS
  update followed by reinstallation restores the menu integration.
  **Status:** implementation and automated checks added. Discovery queries the
  launcher interpreter without importing FPMS. Tests cover stale directories,
  Python path shadowing, broken environments, unsupported layouts, and
  reinstallation after a simulated package update. The full suite passes
  349 tests on macOS with Python 3.13.3. On-device verification is pending;
  follow [the discovery checks](pi_testing.md#fpms-package-discovery-acceptance).

- [ ] **3. Adapt front-panel controls to current FPMS behavior.** Decide how
  Utilization should handle Key 2, now used by FPMS for display rotation, while
  preserving Key 3 screenshots. Keep GPIO and display hardware access in FPMS.
  **Acceptance:** verify the agreed controls, rotation behavior, screenshots,
  and return to the built-in menu; screen changes retain capture and history.

- [ ] **4. Discover capture interfaces and network namespaces.** Replace the
  front-panel adapter's hardcoded `wlan0` with configurable or discovered radio
  selection. Detect namespaced interfaces and support their command context
  consistently for capture, capabilities, tuning, and survey reads.
  **Acceptance:** test renamed interfaces, multiple adapters, and a namespaced
  radio; missing or unavailable interfaces produce a useful message.
  **Status:** the front-panel default now uses `wlanpi0`, with an explicit
  `WLANPI_UTILIZATION_IFACE` override to unblock item 1 testing. Automatic
  discovery and namespace support remain pending.

- [ ] **5. Coordinate radio use and restore state.** Detect conflicting use by
  Core, Profiler, Kismet, or another capture. Reuse a suitable available monitor
  interface where possible, track resources owned by Utilization, and restore
  changed interface state on exit and startup failure.
  **Acceptance:** active tools are not disrupted; display and logging-only exits,
  interrupts, and failed startup clean up correctly without removing monitor
  interfaces owned by other tools.

- [ ] **6. Align Python setup with Trixie.** Require an isolated environment in
  the development setup script, retain the production `/opt` environment, and
  align support metadata and documentation with the Python 3.13 target.
  **Acceptance:** fresh development and production installs, CLI launchers,
  hardware-free replay, and the full pytest suite work on Python 3.13.

- [ ] **7. Validate capture and metrics with TShark 4.4.** Check required fields
  and optional-field negotiation against the shipped version. Use representative
  captures to verify retries, RSSI, HE/EHT widths, puncturing, and survey counters.
  **Acceptance:** compare live results with the same saved capture across
  supported 2.4/5/6 GHz adapters; unsupported widths and unavailable survey data
  remain explicit. Add small sanitized regression fixtures where useful.

- [ ] **8. Expand diagnostics and finish release documentation.** Collect OS
  and package versions, active FPMS paths, service status, relevant journal
  output, interfaces, and namespaces. Correct the existing FPMS 2.x references
  and document the tested Trixie installation and troubleshooting steps.
  **Acceptance:** diagnose an installation or capture failure from the collected
  output, then verify fresh installation, reboot, and repeat installation on
  DeadEye before publishing the Trixie release.

## Architecture constraints

Preserve one capture and analysis pipeline, shared immutable snapshots,
automatic BSSID selection, and the continuous two-minute history. Keep QBSS
and survey utilization separate, and advertised station counts separate from
observed client counts. New OS integration belongs at the hardware and UI
boundaries; follow [AGENTS.md](../AGENTS.md).

Baseline references: [DeadEye release notes](https://github.com/WLAN-Pi/pi-gen/releases/tag/26.10-DeadEye)
and [FPMS changelog](https://github.com/WLAN-Pi/wlanpi-fpms/blob/6ec8d92/debian/changelog).
