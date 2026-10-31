# Utilization roadmap

Updated October 30, 2026. The next phase is **wlanpi-core expansion and default
support**. Its proposed release target is **2.2.0**; implementation now uses
`2.2.0.dev0`. The released baseline is 2.1.0. Direct capture remains the default
until the implementation and acceptance gates below are complete.

## Completed baseline

The 2.1.0 scope is complete: one managed capture/analyzer shared by web, CLI and
FPMS, authenticated web access, Hosts, independent logging and beacon pcapng
export, and accepted device smoke checks including normal radio restoration.
External WiFi-Beacon-analysis validation remains deferred.

Remote refs checked October 30 confirm `v2.1.0` at `83f25de` and `main` at
`87b4f85`, after the release merge. A Git tag does not establish GitHub release
assets or PyPI publication. Development package metadata is now aligned at
2.2.0.dev0; the tagged 2.1.0 baseline is unchanged.

Use the [2.1.0 release notes](release-2.1.0.md) for delivered behavior, the
[managed runtime history](managed-runtime-roadmap.md) for implementation and
acceptance evidence, and the [Trixie history](trixie-roadmap.md) for the completed
platform migration. Their historical next steps and backend decisions are
superseded by this roadmap.

## Version plan

Use **2.2.0** for expanded Core support and the default transition while
preserving explicit direct capture, existing commands/configuration and metric
contracts. This is a minor feature release under
[Semantic Versioning](https://semver.org/). `pyproject.toml`, `setup.py` and
`beacon_live.__version__` now agree at `2.2.0.dev0`; use
`2.2.0rc1` for release-candidate device acceptance and `2.2.0` for the accepted
release. Reassess major-version requirements if the scope removes supported
commands, configuration or direct operation.

## Core capture and radio management

Core-default capture means that a new supported WLAN Pi installation uses Core
to tune and capture packets unless an explicit backend setting selects direct.
Core streams one unfiltered PCAPNG source into Utilization's local TShark
decoder and existing analyzer. The managed broker still owns Utilization's
session, history, renderers, logging and export.

API-managed radio operation additionally delegates the complete radio lifecycle
to Core. A physical radio (phy) may have several interfaces; reserving a capture
interface alone does not protect its siblings from another tool or controller.

| Responsibility | Core-default capture with the current integration | Complete API-managed radio operation |
| --- | --- | --- |
| Packet capture and tuning | Core owns capture and applies the request; Utilization verifies actual tuning. | Core also provides authoritative actual-state readback. |
| Radio reservation | Utilization retains its local phy lock and conflict checks; external tools do not share that lock. | Core provides a radio reservation contract covering siblings and cooperating controllers. |
| Temporary preparation | Utilization saves state and prepares idle sibling interfaces locally. | Core saves state and prepares the radio within the reservation transaction. |
| Stop and restoration | Utilization stops Core, confirms release, then restores its saved local state. | Core restores saved state and reports completion or a specific failure. |
| Discovery and survey | Local discovery/capability/survey reads remain where API coverage is insufficient. | Core provides validated identity, namespace, capability and survey data. |
| Abrupt failure | Disconnect can stop Core capture; saved local radio state may remain unrestored after owner death. | Core defines recovery after client death and service restart; power loss/device failure still need explicit limits. |

For example, Core can stop Dumpcap after a client disconnects while leaving the
radio on the capture channel. That proves capture cleanup, not restoration of
the previous channel and sibling interface states. Retain the local guard until
the relevant Core lifecycle services exist and are accepted. Details and pinned
upstream references are in the [Core evaluation](core-capture-evaluation.md).

## Ordered Core milestones

Milestone 1 has a reviewed source contract, CI implementation and device matrix.
Current-image inventory remains pending; milestones 2–6 remain unimplemented.
No backend default has changed.

Every milestone preserves the [architecture rules](../AGENTS.md): one capture
and analyzer, shared immutable snapshots and 120-second history, automatic BSSID
selection, latest-beacon authority, separate QBSS/survey and advertised/observed
client metrics, and capture-timestamp bucket closure with the 102.4 ms allowance.
Consumers continue rendering published metrics without independent aggregation.

### 1 Establish the supported Core contract

- [ ] Verify the installed Core/API version and shipped capture protocol; record
  a minimum supported version and tested revisions. **Source review complete:**
  [support contract](core-support.md) records the 2.4.8-1 device-tested floor and
  current `63c5e7e` source review. Installed-image inventory/probe is still pending.
- [x] Define supported interfaces, namespaces, bands, primary channels, widths,
  centers, credentials/certificate handling, and local radio responsibilities.
  Use available capability data and explicit tested limits rather than assuming
  a version number guarantees support.
- [x] Establish repeatable CI for hardware-free parser/analyzer, owner transport,
  broker and web tests. Identify required tools so optional integration skips
  are visible rather than counted as acceptance.
- [x] Record a device matrix: Pi model, OS/Core/FPMS/WebUI/TShark versions,
  adapter/driver, namespace, band/primary/width/centers, controls exercised,
  restoration result and retained evidence.

The [support contract](core-support.md) distinguishes upstream API availability
from app support and physical acceptance. The [device matrix](core-device-matrix.md)
preserves prior limited evidence and supplies the current-image inventory steps
and run record. CI is defined in `.github/workflows/ci.yml` and uses
`scripts/run_ci.py`; local validation is recorded in the support contract. Its
first hosted GitHub execution remains pending a push/PR.

**Exit:** an explicit support matrix and repeatable baseline distinguish app
work from upstream Core requirements. No new metric or display is prerequisite.

### 2 Make configuration respect backend capabilities

- [ ] Offer compatible sources and channel definitions in web/FPMS setup and
  validate CLI/managed requests before radio mutation.
- [ ] Resolve or explicitly exclude upper-5-GHz mapping gaps. Expand beyond the
  current app's 20/80 MHz Core support only after center/direction verification.
  80+80, 320 MHz and puncturing require upstream support before being offered
  through Core.
- [ ] Use one backend/default policy across entrypoints, with documented saved
  configuration and environment precedence. Preserve explicit direct choices.
- [ ] Reuse validated Core discovery and actual-channel readback where complete;
  keep local checks where required. Unsupported requests give actionable errors.

**Exit:** setup cannot advertise a configuration that its selected backend is
known to reject, and accepted captures have verified actual source/tuning.

### 3 Accept Core lifecycle and metric parity

- [ ] Exercise repeated starts/stops, simultaneous web/CLI/FPMS attachment,
  navigation, logging/export toggles, rotation, buffered closure and normal
  restoration without restarting history.
- [ ] Exercise failed tuning/startup, busy radios, Core restart/disconnect,
  authentication/certificate failures, SIGTERM/service shutdown, decoder
  failure/backpressure and adapter removal. Define owner-death recovery limits.
- [ ] Compare completed interior retry/QBSS intervals and packet/export fidelity
  using the same saved stream. Keep unavailable drop evidence distinct from zero.
- [ ] Measure capture-to-display delay, throughput, CPU/RAM and buffering on Pi
  4/5 under sustained comparable traffic, with export and consumers enabled.

**Exit:** the supported matrix has saved evidence of correct metrics, bounded
failure behavior and restoration. A smoother display alone is insufficient.

### 4 Add compatible read-only subscription

- [ ] Discover and explicitly join a matching Core stream with exactly one
  source, one fixed channel, verified namespace/tuning and an empty filter.
  Reject hopping, filtered, incompatible or ambiguous sessions.
- [ ] Publish Core role, Core session ID and available controls. Distinguish
  Utilization's managed session from the underlying Core capture session.
- [ ] Subscriber Stop Session ends Utilization's analysis/logging/export and
  detaches its subscription; it never stops, retunes, prepares or restores the
  external owner's radio. Closing a renderer still only detaches that renderer.
- [ ] Handle owner exit, reconfiguration, disconnect and slow-consumer removal
  explicitly. Reconnection must not silently join a changed source or duplicate
  packets; invalidate incompatible coverage and require a new session as needed.

**Exit:** sharing is useful without transferring or interfering with ownership.
Another tool's compatible session is reused only through the supported policy.

### 5 Enable Core by default and release

- [ ] After milestones 1–4 pass, default new supported WLAN Pi installations
  and unspecified backend requests to Core consistently across web, CLI, FPMS
  and the managed owner.
- [ ] Preserve saved explicit backend choices; define migration for existing
  installations and test fresh install, upgrade, reinstall and reboot.
- [ ] Retain explicit direct capture and hardware-free replay. Core errors
  produce a clear failure and recovery guidance, with no silent direct fallback.
- [ ] Align versions and operating/release docs, run final regressions and the
  device matrix, then tag the exact accepted release commit.

**Exit:** Core is the documented and tested default within its declared support
contract. Unsupported hardware/channel features remain explicit.

### 6 Extend Core radio lifecycle APIs

- [ ] Agree upstream contracts for whole-radio reservation, saved-state
  preparation/restoration, identity and administrative-state readback, survey
  counters, and recovery after client/service failure.
- [ ] Adopt each implemented service behind the existing hardware boundary;
  remove the corresponding local operation only after equivalent acceptance.
- [ ] Test conflicts involving sibling interfaces and cooperating tools, lease
  release, restoration failures and recovery without disturbing a new owner.

This upstream track can proceed alongside milestones 1–5 and can continue after
2.2.0. Complete API-managed radio operation is not a prerequisite for Core-default
capture while Utilization retains its tested local safeguards. A Core interface
claim must not be presented as exclusive control over every tool on the system.

## Later work

External WiFi-Beacon-analysis validation, broader survey/noise/SNR hardware
support, richer vendor/radio identifiers, OBSS counts, broader live-platform
support, Grafana and touchscreen hardware remain outside this phase. The
[idea backlog](to_do.md) retains smaller UI/documentation ideas and historical
notes. Adapter reception limits, including missing 6 GHz data, are not solved by
changing the capture provider.
