# Utilization managed runtime release roadmap

This phase targets **v2.1.0**: one managed capture and analyzer session shared
by web, CLI, and FPMS consumers, with a responsive web application and beacon
pcapng downloads. Direct capture remains the default; Core remains optional.
Grafana and touchscreen hardware integration are deferred. A future on-device
browser will open the same application at a local URL.

**Naming accepted October 14, 2026:** use **Utilization** for app titles, FPMS,
terminal views, and the web application at `/utilization/`. Prefer
`wlanpi-utilization` for the device/service command and `utilization` for the
workflow CLI. Keep `wlanpi-beacon-live` and `beacon-live` as compatibility
aliases; retain existing package/configuration/log directories during this
phase so upgrades do not strand files or settings.
New generated log filenames use the `utilization_` prefix; existing files stay
in place and CSV/JSONL metric fields are unchanged.

## Release baseline and versioning

Reviewed October 14, 2026, from `50fc061` on `dev`. The working tree was clean.
Local tags contain only `v1.0.0` Bookworm, but `git ls-remote --tags origin`
confirms **v2.0.0** at `5c6cb090b004cd82e8bbeb4ac11e5d0f7dc04009`.
The remote `main` points to that Trixie merge. A separate shallow checkout of
v2.0.0 confirms its tracked tree equals this checkout's baseline tree
(`d9c5f24bc9126871f26da36c06611ee1e42ed853`). No application changes from that
release need to be imported before this phase. The Trixie
[roadmap](trixie-roadmap.md) and [release checks](trixie-release.md) retain
historical pre-tag acceptance notes; the remote tag is the version baseline.

The managed runtime, web consumer, and export workflow are additive product
features, so the next target is a minor release, v2.1.0. Existing CLI/FPMS
commands and metric meanings remain compatible on the current Python 3.13+
Trixie target. Package metadata in `pyproject.toml`, legacy `setup.py`, and
`beacon_live.__version__` still identifies 0.1.0 even at the v2.0.0 tag.
Use **2.1.0.dev0** during this phase and align those fields; reserve 2.1.0 and
the `v2.1.0` tag for accepted release work. Recheck remote versions before
tagging. This phase does not change the existing release tags or publish one.

## Release baseline implementation

`live.run_live` owns discovery, `RadioSession`, TShark or the optional Core
transport, the ordered analyzer, survey polling, CSV/JSONL logging, and renderer
updates. The Pi aggregation worker preserves a single ordered analysis pipeline.
CLI and FPMS each launch the live runtime; their existing per-phy exclusion
prevents overlapping capture but does not provide a shared service to attach to.
`MetricsSnapshot` and completed `SecondStats` already supply the display and
logging contracts. Screen navigation and logging toggles retain history.

Direct `--raw-pcapng` saves full packets from the same TShark invocation using
`-s 0`, `-P`, and `-w`; normal capture uses a 1024-byte snapshot length. Core
already feeds its original pcapng stream to a local decoder. Neither path yet
provides managed beacon-only export, export rotation, or download inventory.
CSV/JSONL logging already has a bounded worker, hourly rollover, free-space
checks, and skipped-record reporting. JSONL beacon projections cannot recreate
the original packet bytes or radiotap metadata.

## Runtime and consumer contract

One application service owns one session, radio lifetime, source, decoder,
analyzer, survey reader, and logging/export workers. HTTP workers and renderer
connections attach through a publication/control boundary; they never acquire
a radio or instantiate an analyzer. Avoid a runtime per WSGI worker. The
capture owner runs in its own process with graceful signal cleanup; keep the
existing Linux aggregation fork out of a threaded HTTP worker.

Read operations return immutable shared publications with a session ID and
revision. A late or reconnecting consumer receives current state and the same
120-second history immediately. Slow consumers skip intermediate publications
and can detect revision gaps without blocking capture. Disconnecting the last
consumer leaves capture running. Only an explicit authorized stop, source EOF,
failure, or service shutdown ends a session. Completed statistics originate
only from capture-watermark closure, including the 102.4 ms allowance; browser
and wall-clock timers do not finalize retry buckets.

Serialize start/stop and logging/export controls. Reject incompatible starts
while a session owns resources; a band/channel/width change is an explicit
stop and new session with a new ID. Keep final history after stop until that
explicit new session. Surface starting, running, stopping, stopped, and failed
states, capture coverage, collecting status, errors, and logging/export health.
Use existing radio safety/restoration for direct and Core owner sessions.

Preserve latest-beacon authority, automatic BSSID selection/hysteresis,
retry-footer selection, all retry eligibility/address-association rules,
best-effort radio grouping, and existing primary-channel scope. Keep AP QBSS CU
and local survey CU separate, and QBSS stations and observed clients separate.
Use null for unavailable fields, `sample_available=false` for graph gaps, and
`<1%` for nonzero retry percentages below one. A real zero remains zero.
Consumers must not independently aggregate frames or substitute stale values.

## Web application and WLAN Pi integration

Build compact phone views and expanded desktop views from the same published
snapshot and completed rows. Show current utilization, stations, retries,
composition, admission capacity, beacon reception/loss, selected identities,
survey availability, coverage, and update time. Desktop adds synchronized
120-second graphs and a BSSID table based on authoritative snapshot states.
Navigation, table sorting, and graph toggles are presentation operations.

Provide explicit session start/stop, band/channel/width selection, logging and
export toggles, current segment/status, disk warnings, and completed-file
downloads. Offer no SSID/BSSID selector. Use safe session-owned file IDs for
downloads; handle rotation/deletion during a download and exclude incomplete
active files until the download contract is defined.

The existing [WLAN Pi WebUI](https://github.com/WLAN-Pi/wlanpi-webui) uses
Nginx, Gunicorn, and Flask; sign-in uses Core PAM validation for local accounts
in the sudo group. Initially keep Utilization's backend separate. Add a launcher
entry and propose a same-origin `/utilization/` route to a loopback or Unix
socket backend, protected using the installed platform's authentication.
This is an integration design, not an existing authenticated proxy contract.
Verify actual launcher/configuration hooks and session validation on the target
image before choosing the adapter. A launcher link alone does not authenticate
the backend. Protect controls, streams, and downloads consistently, including
CSRF/origin checks and expiry/logout. Never trust browser-supplied identity
headers or expose an unprotected privileged listener. Custom upstream Core
endpoints are optional later work.

Serve assets locally and support a configurable URL prefix so phones, desktops,
and a future local kiosk browser use the same frontend without a hardware UI
fork. Select the web framework and polling/SSE transport in milestone 2; no
frontend dependency or HTTP server is needed for milestone 1.

## Beacon export design

Retain one unfiltered packet source with full packet lengths. Fan its original
pcapng blocks to the existing decoder and a bounded export worker. For direct
capture, evaluate a single Dumpcap source feeding TShark stdin, following the
already tested Core stream/decoder pattern. Do not add a second interface
capture, a beacon capture filter, or a beacon-only analyzer feed. Continue
decoding every required WLAN frame for retries and observed clients.

Classify beacon packets in the export branch, preserving each original packet
payload, timestamp precision, interface mapping, radiotap bytes, and relevant
pcapng options. Preserve/re-emit section and interface definitions in every
independently readable segment. Handle multiple sections/endian forms,
timestamp resolution/offset, malformed blocks, unsupported link types, and
truncated packets explicitly. Do not synthesize packets from `BeaconRecord`
or silently claim that filtered interface statistics describe export counts.

Rotate on configured time/size boundaries with a maximum retained byte/file
budget and minimum free-space reserve. Delete only owned closed segments under
an explicit retention policy. Use temporary active files and atomically close
segments; report exported/skipped packets, queue pressure, disk errors, and
incomplete output. Disk exhaustion or a stalled writer disables export or drops
export records with visible accounting while analysis continues. Never queue
unbounded raw bytes or block decoder delivery on export/download I/O.

## Ordered milestones and acceptance

- [x] **1. Managed session foundation.** Add hardware-free lifecycle and
  publication APIs around `run_live`, carrying original snapshots, completed
  statistics, and renderer status. Implement one active owner, explicit stop,
  retained final state, and independent read-only consumers. Keep CLI/FPMS
  entrypoints working. Verify multiple/late/slow consumers, navigation and
  disconnect independence, overlapping starts, stop during startup, EOF,
  startup/runtime failure, and explicit restart. This bounded milestone stops
  before IPC deployment and HTTP/UI work.
- [ ] **2. Service and consumer attachment.** Host the owner in a managed
  process with local IPC and signal handling; add API serialization and explicit
  controls. Attach CLI/FPMS renderers through that service, with documented
  standalone compatibility. Test simultaneous consumers against one capture,
  logging toggles, service restart/failure, radio restoration, and overload.
  Choose web framework, IPC permissions, transport, service policy, and limits.
  **Implementation and local regression coverage complete; initial Pi results
  reported October 16, including successful CLI stop and correct TUI display.
  Broader device/release coverage remains pending.** See milestone
  2 evidence below and [service checks](managed-service.md).
- [ ] **3. Responsive web consumer.** Deliver current metrics, desktop
  graphs/table, controls, logging status, and CSV/JSONL downloads. Verify phone
  and desktop layouts, null/zero/gap semantics, `<1%`, delayed publications,
  reconnect/navigation, multiple tabs, and local-browser operation.
  **Implementation and local automated coverage complete October 16;
  visual browser and Pi acceptance remain pending.** See milestone 3 evidence
  below and [web operation/checks](web-interface.md).
- [ ] **4. Launcher and authentication.** Implement the verified platform
  launcher and protected route without merging the Utilization backend into
  Core. Test login/logout/expiry, controls/stream/download authorization,
  installation/reinstallation, URL prefixes, and service failure recovery on Pi.
- [ ] **5. Beacon pcapng export.** Implement the same-source export branch,
  rotation, retention, disk isolation, status, and downloads. Compare retained
  beacon bytes/timestamps/radiotap with source packets; decode every segment
  with TShark. Prove analyzer results match with export on/off for a mixed
  beacon/data/retry fixture. Test slow/full disks, queue overflow, malformed
  input, repeated rotation, and export stop/restart without history resets.
- [ ] **6. Offline analysis and release acceptance.** Run the end-to-end
  workflow below with exported segments, record evidence, complete device and
  performance checks, reconcile versions, and prepare v2.1.0 release notes.
  Tagging/publication follows release acceptance.

## WiFi Beacon analysis end to end check

Use a pinned revision of
[WiFi-Beacon-analysis](https://github.com/Yanzzee/WiFi-Beacon-analysis).
Its supported entrypoint is `beacon_analysis.py run`; inputs include native
radiotap pcapng and `Sample_APs.csv`, `Sample_classrooms.csv`, and
`Sample_schedule.csv`. Beacons without a configured AP hostname are outside
scope. Aruba/Extreme AP-name support does not imply every vendor is supported.

Prepare a sanitized mixed-frame source with an AP name supported by the offline
adapter, QBSS values including missing CU, and known epoch timestamps. Supply
unique AP hostnames mapped to classrooms, classroom metadata, and schedule
rows containing matching classroom, start/end, course, enrollment, and capacity
columns according to that pinned revision's CSV schemas. Record project,
capture/output/sample folders, start/end dates, timezone, TShark path/version,
and source/export hashes. Put the capture inside a scheduled interval; document
timezone and date boundaries. AP/classroom/schedule context is external metadata
and is not recoverable from the exported packets alone.

Export at least two closed segments, verify beacon-only contents and original
packet metadata, then run `beacon_analysis.py run --project <name>` with explicit
configuration and the pinned entrypoint's noninteractive flags. Inspect its
manifest, identity map, selected observations, and radio/classroom summaries
for expected in-scope APs, bins, valid and missing CU, and coverage. Retain the
command, revisions, fixtures, hashes, and outputs as acceptance evidence.
Use expectations from the offline pipeline's representative-bin rules; its
class/bin summaries need not equal Utilization's latest-beacon one-second
display. A file opening successfully is only the first part of this check.

## Evidence and remaining decisions

**Milestone 1 complete October 14, 2026.** `beacon_live/runtime.py` provides
`LiveSessionConfig`, `ManagedRuntime`, read-only consumers, and immutable
`RuntimePublication` state. One owner invokes the existing `run_live`; consumers
read/wait without frame queues or capture side effects. Concurrent starts fail
until cleanup returns, stop controls require the current session ID, and stale
publications cannot overwrite a new session. Final state remains readable;
only an explicit new run clears history. Capture must run on the owner's main
thread. Startup stop requests are cooperative and remain pending until polling.

The live loop now has optional capture-start, completed-statistics, and final
snapshot publication hooks. Existing terminal/LCD dashboards need no new
methods. Snapshot cadence and completed-statistics cadence remain independent;
the runtime retains original completed rows without recomputing metrics. CLI
and FPMS continue using their working foreground entrypoints. No daemon, HTTP
endpoint, frontend, launcher/auth adapter, or beacon exporter is installed by
this milestone. Package version fields now agree at `2.1.0.dev0`; legacy setup
metadata also agrees with the Python 3.13 target and current optional extras.

**Automated evidence on Python 3.13.3:** baseline `pytest -q` passed 624 tests
with one skip. The final `pytest -q -rs` run passed **640 tests with one skip**
in 18.94 seconds. The skip is the existing optional Core transport integration
requiring `websockets`, absent in this local environment. Existing Core mocked
and decoder regressions run in the suite. New tests cover multiple/late/slow
consumers, unchanged shared history during navigation and disconnect, lifecycle
conflicts, stop during startup and cleanup, errors/EOF/restart, immutable state,
stale controls/publications, bounded completed-row retention, and consumer
wait/close. Managed integration checks exercise both the inline fallback and
the real forked aggregation worker using mixed beacon/data/retry input: one
capture, preserved one-second rows, 50% retry ratio from one retry of two
eligible frames, unavailable survey data, and explicit graph gaps. The stop
check verifies capture termination and the final analyzer snapshot. Metadata
consistency and `git diff --check` also pass. A separate CLI replay smoke check
with `samples/tshark_qbss_sample.tsv` reads five valid beacon rows, skips zero
malformed rows, and produces the expected two statistics rows.

**Milestone 2 implementation October 14, 2026.** The private Unix-socket broker
owns one isolated capture process, with versioned bounded JSON IPC and explicit
start/join, stop, and logging controls. The owner runs `run_live` on its main
thread without a service publication/control thread before the analyzer fork.
Consumers read cached publications and render the existing snapshot. The
nonblocking owner link coalesces pending publications; client queues and
requests are bounded. A duplicate broker cannot replace an active socket.
Failures retain their status, and service shutdown waits for owner cleanup.

Preferred commands and terminal titles now use Utilization. Installers create
the preferred launchers and preserve legacy aliases, including upgrades of
legacy managed wrappers. `--managed`, `--attach`, and `--start-only` provide
CLI entrypoints. The systemd unit starts idle; capture starts only explicitly
and does not resume automatically after service restart/reboot. Socket mode
0600 and directory mode 0700 restrict local privileged controls. Managed FPMS
is an opt-in persistent setting via `--fpms-runtime managed`; its display exit
detaches, Stop Logging preserves capture, and Stop Capture is explicit.
Standalone CLI/FPMS behavior remains available and is still the initial default.

**Decisions resolved:** separate broker/capture processes; local JSON IPC with
an 8 MiB message limit, 16 clients, and five-second request deadlines; owner
and socket root access on Pi; explicit idle/start/stop policy; independent
backend/runtime choices; one-second publication polling with fast local
navigation; and Flask with local assets/JSON polling for the next web consumer.
Service installation/reinstallation explicitly restarts the broker and ends
an active session. The platform login adapter and the future web backend's
access to the privileged local socket remain deployment decisions.

**Milestone 2 evidence:** process tests cover multiple clients with one owner,
preserved snapshots/retry rows, navigation/detach, logging on/off, source
conflicts, stale controls, startup error, unexpected owner exit, owner cleanup
on service SIGTERM, duplicate services, malformed/oversized IPC, and bounded
partial writes for a slow reader. A real CLI subprocess attaches through IPC,
changes its LCD screen, and exits while the owner/history remain active. FPMS
tests cover managed commands/menu, retained logging on display exit, invalid
runtime selection, and saved settings across reinstallation. Production-copy
tests execute help for all preferred/legacy launchers and retain replay/spawn
safety. These fixtures test process cleanup; physical radio restoration,
systemd/reboot installation, optional Core lifecycle, and high-utilization Pi
performance still require the [device checks](managed-service.md).

The final regression run on Python 3.13.3 passed **661 tests with one skip**
in 16.48 seconds. The skip remains the optional Core transport integration
requiring `websockets`. CLI help for live/service/session commands, hardware-free
replay, Python compilation, installer shell syntax, metadata/entrypoint
consistency, and `git diff --check` also pass. Unix-listener process tests ran
outside the filesystem sandbox because it prohibits binding a local listener;
they use private temporary sockets and no Wi-Fi hardware. These local checks
did not perform Pi installation, systemd deployment, web exposure, or release
publication; subsequent Pi reports are recorded below.

At milestone 2 completion, the next bounded work was the responsive web
consumer at `/utilization/` using this service boundary. Its implementation is
recorded below. Platform authentication/launcher hooks, export source plumbing,
timestamp/block support, export disk budgets, and the offline-analysis
revision/contextual fixture remain later decisions.

**Pi report October 16, 2026:** the user reports that everything appears to
work except the CLI stop example. The supplied transcript from `wlanpi-bd2`
shows idle state followed by a running managed session with CSV/JSONL logging
active. The session ID and active logging remain unchanged after CLI attach
returns. A request for channel 6 is rejected as a source conflict. The
diagnostic collector reports 43 checks with zero failed, unavailable, or timed
out; the archive and service/FPMS journal were generated but have not been
reviewed here.

The stop attempt supplied the literal `SESSION_ID` placeholder. Its stale-ID
rejection is expected and does not establish a stop implementation failure.
README/service examples now retrieve the actual ID automatically. In a
follow-up on October 16, the user confirmed that stop worked with the actual
ID and that the TUI display looked correct. This accepts the reported stop
operation and TUI behavior for progressing to the web consumer. Physical
radio-state restoration, reboot/reinstallation, exact band/width coverage, and
high-utilization measurements were not detailed and remain release coverage.

## Milestone 3 implementation and evidence — October 16, 2026

Implemented the separate optional web consumer in `beacon_live/web.py`, with
local HTML/CSS/JavaScript assets at `/utilization/`. Flask/Waitress serves a
loopback-only interface; HTTP threads share a one-second publication read cache.
`wlanpi-utilization web` never creates a capture owner or analyzer. Page loads,
navigation, sorting, reconnects, and additional clients perform read operations.
Explicit start/stop/logging controls use the existing service and session IDs.
Direct capture remains the form default; Core remains optional. CLI/FPMS
standalone and managed attachment behavior are preserved.

The page displays eight current metric cards, rolling composition, six desktop
graphs, a phone graph selector, a sortable BSSID table, capture/collecting and
logging status, notices, and closed-file downloads. Metric values come from
`MetricsSnapshot` and existing screen projections. Original completed
`SecondStats` remain in the status response. Null/zero/gap semantics and `<1%`
retry presentation are preserved; no browser clock closes analyzer buckets or
slides history. Disconnection retains the last values with explicit status and
disabled controls. No BSSID/SSID selection or metric aggregation was added.

The download decision is **closed registered CSV/JSONL segments only**. The
logging worker records original descriptor identities after flushing/closing;
the broker keeps a bounded catalog across sessions. HTTP downloads verify
root containment, no symlinks, identity, size, and modification time, then
stream from the opened descriptor independently of capture. Active, replaced,
truncated, missing, and out-of-root files are excluded or unavailable. An open
download survives unlink. Catalog limits and restart behavior are documented
in [web operation](web-interface.md); no historical-directory scan was added.
Beacon pcapng export, export retention, and offline analysis remain later work.

Added the optional `web` dependency extra and `--with-web` Pi installer flag.
Web assets are included in both production copies and package wheels. The
initial deployment uses the existing private root-owned broker through
`sudo wlanpi-utilization web`, with local access or SSH tunneling. Socket
permissions remain private. Origin/host/CSRF checks and local-only assets are
implemented; the platform login/launcher adapter and remote deployment remain
milestone 4. This milestone does not enable a web systemd service or modify
WLAN Pi's Nginx/authentication configuration.

Evidence on Python 3.13.3:

- Full regression: **678 passed, one skipped in 16.97 seconds**. The skip is
  the optional Core transport integration requiring `websockets`. The web,
  service, logging, and installer group separately passed 54 tests.
- The real Unix broker/synthetic owner test covers idle page load, explicit
  web start, multiple clients on one ID/snapshot, active-file exclusion,
  logging stop and actual CSV/JSONL downloads, unchanged analysis/history,
  explicit stop/restart, and retained files with their original session IDs.
- API tests cover original null/zero/gap/completed values, stale controls,
  missing service/reconnect, origin/CSRF/host/body limits, URL prefixes, and
  registered download traversal/symlink/replacement/truncation/unlink races.
  Logging tests verify rollover/low-disk inventory and original descriptor
  identity even when the path is replaced before close. Installer tests cover
  isolated web dependencies, failure preserving an installed app, copied
  assets, and web help without installed optional dependencies.
- JavaScript syntax and a synthetic-DOM execution check passed for six graphs,
  three BSSIDs, gap paths, `<1%`, zero versus null, read-only navigation/sorting,
  graph selection, disconnect/reconnect, retained values, idle state, and
  control capabilities. This is a logic check, not browser rendering evidence.
- Package wheel build and asset/optional-dependency metadata checks passed.
  CLI help/replay, Python compilation, installer shell syntax, and
  `git diff --check` passed. Local-listener regression tests ran outside the
  filesystem sandbox, with temporary private sockets and no radio hardware.

Visual browser verification was attempted with the hardware-free
`tests/fixtures/web_preview.py` fixture. Native Chrome and Brave control timed
out in this environment, so there is **no claimed desktop/phone visual pass**.
The responsive rules are implemented; actual browser rendering, accessibility
interaction, local on-device browser operation, Pi radio restoration, and
performance are still acceptance gates. The fixture and concrete Pi checks are
documented in [development](development.md) and [web acceptance](web-interface.md#pi-browser-acceptance).

**Next bounded work:** run those browser/Pi checks and implement milestone 4's
verified launcher/authentication adapter. Resolve platform session validation,
logout/expiry, same-origin route and trusted proxy handling, and least-privilege
backend access against the installed WLAN Pi image before exposing remote
controls. Keep the backend separate and retain this frontend for future local
browser use. Grafana and touchscreen hardware integration remain deferred;
the release version stays `2.1.0.dev0` pending later milestones and acceptance.
