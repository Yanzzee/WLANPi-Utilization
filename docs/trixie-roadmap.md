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

- [x] **2. Locate the active FPMS installation reliably.** Resolve the package
  through the interpreter used by the FPMS launcher instead of globbing every
  Python directory. Detect unsupported layouts and document reinstallation
  after FPMS package updates.
  **Acceptance:** stale Python directories do not affect discovery; an FPMS
  update followed by reinstallation restores the menu integration.
  **Status:** complete. Discovery queries the launcher interpreter without
  importing FPMS. Tests cover stale directories,
  Python path shadowing, broken environments, unsupported layouts, and
  reinstallation after a simulated package update. The full suite passes
  349 tests on macOS with Python 3.13.3. On October 12, 2026, the user confirmed
  on-device testing after item 2 was good. A separate hex SSID display issue
  is recorded below.

- [x] **3. Adapt front-panel controls to current FPMS behavior.** Decide how
  Utilization should handle Key 2, now used by FPMS for display rotation, while
  preserving Key 3 screenshots. Keep GPIO and display hardware access in FPMS.
  **Acceptance:** verify the agreed controls, rotation behavior, screenshots,
  and return to the built-in menu; screen changes retain capture and history.
  **Status:** the user chose Key 2 rotation while capture continues. The adapter
  now calls FPMS's native orientation function and immediately redraws the
  current frame. Key 1 stays disabled; Key 3 still saves screenshots. Regression
  tests exercise the upstream rotation function, saved orientation, repeat
  installation, and retained capture/navigation state. The full suite passes
  357 tests on macOS with Python 3.13.3, including physical button remapping in
  both orientations and after rotation during an event batch. On October 12,
  2026, the user confirmed item 3 testing looks good. Item 3 is complete.

- [x] **4. Discover capture interfaces and network namespaces.** Replace the
  front-panel adapter's fixed interface default with configurable or discovered radio
  selection. Detect namespaced interfaces and support their command context
  consistently for capture, capabilities, tuning, and survey reads.
  **Acceptance:** test renamed interfaces, multiple adapters, and a namespaced
  radio; missing or unavailable interfaces produce a useful message.
  **Status:** automatic discovery and namespace routing are implemented.
  Discovery prefers an existing monitor interface supporting the selected
  frequency and reports ambiguity when multiple candidates are equally suitable.
  Explicit interface/namespace overrides remain available. Hardware commands and
  log metadata retain the selected namespace. Automated checks cover renamed
  interfaces, multiple radios, decimal iw frequencies, unavailable namespaces,
  and one shared capture pipeline. The full suite passes 381 tests on macOS
  with Python 3.13.3. On October 12, 2026, the user confirmed testing looks good.
  Item 4 is complete; the [discovery checks](pi_testing.md#capture-interface-and-namespace-acceptance)
  remain available for additional hardware configurations.

- [x] **5. Coordinate radio use and restore state.** Detect conflicting use by
  Core, Profiler, Kismet, or another capture. Reuse a suitable available monitor
  interface where possible, track resources owned by Utilization, and restore
  changed interface state on exit and startup failure.
  **Acceptance:** active tools are not disrupted; display and logging-only exits,
  interrupts, and failed startup clean up correctly without removing monitor
  interfaces owned by other tools.
  **Status:** complete for the direct-capture backend. A per-phy lock excludes
  concurrent Utilization sessions. Preflight checks inspect every sibling
  interface for packet capture, a connection, AP/other modes, and network
  controllers. Existing monitor interfaces are reused; idle siblings are
  temporarily paused. Cleanup restores saved modes, administrative states,
  and known monitor channel definitions after normal exit, Ctrl+C, SIGTERM,
  and startup failure. Existing interfaces and namespaces are preserved;
  identity checks protect replacement hardware. External tools do not share
  the app's lock, so cross-tool coordination remains a preflight check rather
  than an enforced system-wide lease. The full suite passes 414 tests on macOS
  with Python 3.13.3. Follow [the radio checks](pi_testing.md#radio-ownership-and-restoration-acceptance).
  **Startup regression:** the user's October 12 journal confirms exit 2 because
  NetworkManager has no device object for the existing `wlanpi0` monitor. The
  guard now reads NM's inventory before querying ownership, skipping interfaces
  NM does not list while retaining managed-sibling and inspection-failure checks.
  Regression coverage includes root/named namespaces and a non-English caller
  locale. The full suite passes 421 tests on macOS with Python 3.13.3.
  The subsequent device journal confirms this check now passes.
  **LLDP regression:** the next device journal confirms the NetworkManager
  check now passes and the remaining refusal names `lldpd`. The socket check
  now exempts identified `lldpd` ownership only on managed interfaces. It still
  rejects other/shared owners, unidentified sockets, monitor/global sockets,
  connected siblings, and controllers. Tests cover root/named namespaces,
  successful preparation/restoration with LLDP present, and simultaneous real
  capture owners. The full suite passes 433 tests on macOS with Python 3.13.3.
  On October 12, 2026, the user confirmed the issue is resolved and testing
  looks good after both startup corrections.
  **Architecture assessment:** retain the tested direct backend for this
  migration. Core 2.4.8 already exposes
  authenticated `WS /api/v1/streaming/capture`, with owned sessions and read-only
  subscribers; the REST capture API is still a design. The current backend
  launches capture directly and does not register with Core's session manager.
  The assessment considered fixed-channel unfiltered input, latency,
  namespace/name restrictions, survey reads, and state restoration. Core's
  existing WebSocket does not supply a system-wide phy lease or restore prior
  tuning, and accepts only `wlanpiN` names and 20/40/80/160 MHz widths. A Core
  backend would therefore require additional integration and feature decisions;
  it is tracked separately below. The current backend remains explicitly
  independent of Core's capture registry.
  References: [shipped capture contract](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/docs/capture-ws-mcp-handover.md)
  and [Core usage policy](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/wlanpi_core/services/network_namespace_service.py#L700).

- [x] **6. Align Python setup with Trixie.** Require an isolated environment in
  the development setup script, retain the production `/opt` environment, and
  align support metadata and documentation with the Python 3.13 target.
  **Acceptance:** fresh development and production installs, CLI launchers,
  hardware-free replay, and the full pytest suite work on Python 3.13.
  **Status:** implemented and validated on macOS with Python 3.13.3. Metadata
  requires Python 3.13+. Development setup rejects missing/fake venvs, older
  interpreters, and inherited system site-packages before pip runs; package
  failures stop without a system/non-editable fallback. It exercises CLI help,
  replay, and pytest. The production `/opt` environment is retained and verified
  before copying app files. Automated checks cover setup failures and a fresh
  production install/reinstall with actual launchers and replay. A fresh editable
  development install also passes both CLI/replay checks and all 446 tests;
  the existing environment passes the same full suite. The user confirmed
  successful testing after deploying item 6 on October 12, 2026.

- [x] **7. Validate capture and metrics with TShark 4.4.** Check required fields
  and optional-field negotiation against the shipped version. Use representative
  captures to verify retries, RSSI, HE/EHT widths, puncturing, and survey counters.
  **Acceptance:** compare live results with the same saved capture across
  supported 2.4/5/6 GHz adapters; unsupported widths and unavailable survey data
  remain explicit. Add small sanitized regression fixtures where useful.
  **Status:** implemented; all 472 tests pass on Python 3.13.3. Packet decoding
  was verified with TShark 4.4.19 and 4.6.0. The user confirmed successful
  production installation, all 52 fields, and all four packet checks with
  TShark 4.4.19 on the Pi, and reported good testing so far. This accepts the
  compatibility step; exhaustive live band/width and survey coverage remains
  device-specific. Field inspection now has a bounded timeout, reports probe
  failures, validates core fields
  before radio discovery/preparation, and negotiates optional AP metadata.
  Original synthetic PCAPs and actual TShark 4.4.19 exports cover 2.4 GHz HT40,
  5 GHz VHT160, 6 GHz HE80/EHT320, puncturing, RSSI, QBSS, and repeated retries
  through the shared parser/analyzer. Negative iw width declarations no longer
  imply support. Survey parsing recognizes inline `[in use]`, pairs counters
  by channel identity, and rejects impossible busy/active deltas. A read-only
  packet check needs no pytest or wireless hardware. Follow
  [the TShark 4.4 checks](pi_testing.md#tshark-44-and-metric-acceptance) for device
  validation; synthetic packet decoding does not establish driver coverage.

- [ ] **8. Expand diagnostics and finish release documentation.** Collect OS
  and package versions, active FPMS paths, service status, relevant journal
  output, interfaces, and namespaces. Keep references aligned with FPMS 1.4.24
  and document the tested Trixie installation and troubleshooting steps.
  **Acceptance:** diagnose an installation or capture failure from the collected
  output, then verify fresh installation, reboot, and repeat installation on
  DeadEye before publishing the Trixie release.
  **Status:** diagnostics and release documentation are implemented; all 485
  tests pass on Python 3.13.3. Final device acceptance is pending. The collector uses bounded read-only commands,
  records failures in a manifest/summary, resolves FPMS without importing its
  hardware code, compares installed app/adapter hashes with the checkout, and
  collects root/named-namespace radio evidence. Hardware-free tests reproduce
  failed startup, stale deployment, missing commands, permission errors, and
  timeouts. No obsolete FPMS 2.x references remain in the user guides. Follow
  [the final release checks](trixie-release.md#final-device-acceptance) before
  marking this item complete or publishing the release.

## Separate issues

- [ ] **Optional Core capture backend.** Evaluate using Core's shipped capture
  WebSocket as an owner or subscriber, feeding one unfiltered fixed-channel
  stream into the existing analyzer. Resolve authentication, buffering/latency,
  source-name and width restrictions, local survey context, radio preparation,
  and restoration. Do not claim a system-wide lease from its per-interface
  capture registry. This is separate from completing the direct-backend Trixie
  migration.

- [ ] **SSID names display as hex.** Reported during on-device testing after
  item 2. Investigate TShark SSID output and parser normalization separately
  from front-panel controls, using a known SSID and a sanitized capture/output
  sample. Verify readable names in the LCD, CLI, and logs; retain correct hidden
  SSID and non-text byte handling. Item 7 confirmed that TShark 4.4.19 reports
  `wlan.ssid` as FT_BYTES and emits the synthetic `Trixie-Test` name as hex.
  Normalization is still deferred; no SSID fix is included in item 7.

## Architecture constraints

Preserve one capture and analysis pipeline, shared immutable snapshots,
automatic BSSID selection, and the continuous two-minute history. Keep QBSS
and survey utilization separate, and advertised station counts separate from
observed client counts. New OS integration belongs at the hardware and UI
boundaries; follow [AGENTS.md](../AGENTS.md).

Baseline references: [DeadEye release notes](https://github.com/WLAN-Pi/pi-gen/releases/tag/26.10-DeadEye)
and [FPMS changelog](https://github.com/WLAN-Pi/wlanpi-fpms/blob/6ec8d92/debian/changelog).
