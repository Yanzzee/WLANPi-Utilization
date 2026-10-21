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
The optional milestone 4 adapter now uses the reviewed platform auth subrequest
and a private Unix backend; installed-image acceptance is still required.
A launcher link alone does not authenticate the backend. Protect controls,
streams, and downloads consistently, including
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
  Pi 5 display and speedtest retest accepted October 19 with different test
  conditions. Broader browser/device/performance coverage remains pending.** See milestone 3 evidence
  below and [web operation/checks](web-interface.md).
- [ ] **4. Launcher and authentication.** Implement the verified platform
  launcher and protected route without merging the Utilization backend into
  Core. Test login/logout/expiry, controls/stream/download authorization,
  installation/reinstallation, URL prefixes, and service failure recovery on Pi.
  **Implementation and local checks complete October 19; Pi 4 Apps,
  login/logout/expiry, session attachment and web startup after reboot/relogin
  confirmed October 20. FPMS Stop Session was confirmed working October 21;
  subsequent menu refinements and broader failure/download/release checks
  still need device acceptance.** See milestone 4
  evidence below and [protected deployment](web-deployment.md).
- [ ] **5. Beacon pcapng export.** Implement the same-source export branch,
  rotation, retention, disk isolation, status, and downloads. Compare retained
  beacon bytes/timestamps/radiotap with source packets; decode every segment
  with TShark. Prove analyzer results match with export on/off for a mixed
  beacon/data/retry fixture. Test slow/full disks, queue overflow, malformed
  input, repeated rotation, and export stop/restart without history resets.
  **5a complete October 20:** bounded hardware-free reader/filter/writer and
  offline helper, with original packet-block fidelity, TShark decoding and
  unchanged mixed-frame analyzer results. Live source fan-out, worker/disk
  isolation, rotation/retention, status and downloads remain 5b. See
  [export foundation](beacon-export.md).
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

## Pi 5 follow-up: busy-channel hardening — October 16, 2026

The user reports that basic web operation looks good on Raspberry Pi 5 with
**direct capture**, but download speedtests can inflate observed MAC clients
above 1,000, produce unexpected/hidden BSSIDs, and sometimes leave other
metrics unavailable. The behavior was intermittent and not observed on the
user's Pi 4. Adapter/driver and a raw capture from the affected run were not
available, so the actual incident remains subject to Pi retest.

Implemented two relevant guards. Normal live parsing now retains negotiated
radiotap/TShark FCS quality, enables checksum validation where possible, and
keeps known corrupt headers out of identities and metric inputs. Missing or
unverified FCS is accepted. The multi-occurrence parser now counts actual WLAN
type occurrences and does not copy ambiguous singleton addresses/Retry bits
between headers. Repeated address fields cannot manufacture beacon frames.
Valid-frame definitions and automatic selection are unchanged; no counter cap
was added. New capture-quality counters and timestamp age expose corruption,
rejected rows and decoder delay without finalizing buckets or restarting capture.

The packet-level reproduction with real TShark 4.6.0 changed from **1,101
clients / 201 BSSIDs** using the previous parser to **one client / one BSSID**
with the guard. The fixture has one real AP/client pair and 1,300 damaged
frames. Tests exercise driver bad-FCS flags and actual incorrect CRCs, plus
unknown FCS, replay/streaming parity, repeated fields and timestamp validity.
This confirms a software failure path, not the cause of the user's exact run.

Band-specific primary-channel dropdowns and server-side grid validation are
implemented. The new hardware-free benchmark verifies 120 seconds of exact
metrics and retained history. Its laptop result is about 92k generated/parsed/
analyzed frames per second and 23 MiB peak RSS; it excludes radio, TShark, IPC
and UI costs and is not Pi capacity evidence. Before/after numbers and the
short same-source Pi capture procedure are recorded in
[busy-channel validation](busy-channel-validation.md).

Launcher/authentication work advanced to verified upstream hook review at
WebUI revision `2b112f5a739e4647de4f5fd48948cd16e4d07d94`: an existing
`/auth/check` subrequest and Apps-card template can support a separate backend.
A read-only installed-platform preflight now records paths/hashes/candidate
hooks, also included in the diagnostic bundle alongside managed status/journal
and WebUI/Nginx versions. Actual protected proxy/service/launcher deployment
remains milestone 4 after installed-image validation; no remote listener or
authentication bypass is enabled by this hardening milestone.

**October 19 Pi 5 evidence:** the user reports a good display and a speedtest
without the previous anomalies, and accepts proceeding. Conditions differed;
this does not prove exact replication or establish the cause of the original
spikes. Adapter/driver identification and controlled Pi 4/5 stage performance
remain open evidence. Retain a short raw capture if spikes recur. Complete
channel/quality browser coverage alongside protected-hosting acceptance.
Release metadata stays `2.1.0.dev0`.

**Hardening validation:** Python 3.13.3 full regression passed **708 tests with
one optional Core transport skip in 17.15 seconds**. Real TShark tests cover
both live and offline decoders with driver-reported bad FCS and incorrect CRCs.
JavaScript syntax/simulated-DOM checks passed for channel changes, quality/age
reporting, six existing graphs and `<1%`. CLI replay/help, compilation and diff
checks passed. The later Pi retest is recorded above; broader real-browser
acceptance remains pending.

## Milestone 4 implementation and evidence — October 19, 2026

Added a full-page **Utilization** Apps card, protected `/utilization/` Nginx
route, optional `wlanpi-utilization-web.service` and reversible integration
installer. The existing root-only capture broker and CLI/FPMS behavior remain
unchanged. The web service listens only on a private root:`www-data` Unix
socket and consumes the same publications; browser navigation and login
lifetimes cannot stop capture or reset history. Direct remains the default,
Core optional, with Grafana and touchscreen hardware deferred.

The supplied Pi 5 preflight auth-module and Nginx hashes match reviewed
upstream sources. The installer accepts that Pi application revision and the
reviewed upstream revision, with AST checks on freshness/login functions.
Unknown authentication/config revisions are refused for review. Managed-block
edits preserve custom Apps cards, repeated installation reuses the private
token, and syntax/startup/readiness/reload failures roll back files and web
service state. It never restarts the broker or FPMS. General application
updates through the existing FPMS installer still restart those services;
[deployment instructions](web-deployment.md) distinguish the two steps.

Every proxied route uses existing `/auth/check` session validation. The backend
requires a private proxy token, and controls also check CSRF and the HTTPS
origin. Browser-supplied forwarded/token headers cannot establish authority.
Expired/revoked API access returns 401, disables controls and offers normal
platform sign-in; successful login reconnects without starting a new capture.
Polling does not refresh idle sessions. Actual interaction uses the existing
`HEAD /apps` cookie-refresh behavior, throttled to once a minute, while the
platform retains its revocation/idle/max-age policy.

**Local evidence:** the full Python 3.13.3 regression suite passed **728 tests
with one optional Core transport skip in 17.43 seconds**. The installer suite
also passed **16 tests** with warnings treated as errors. The production
Nginx fragment was tested using Nginx 1.28.3 over verified TLS, private Waitress
Unix listeners and a real broker with synthetic capture owner. Checks cover
unauthorized pages/status/controls/downloads, login expiry/revocation, forged
headers, auth-service errors failing closed, closed CSV download and a stable
capture ID/history across clients and renewed access. A controlled auth checker
supplied auth outcomes; actual installed PAM/cookie policy is not claimed.
JavaScript simulated-DOM checks passed for expiry messaging, disabled controls,
retained values, same-session reconnect, POST CSRF/session IDs, activity
throttling, no idle refresh from polling, and rejection before a control when
activity renewal requires sign-in. These are logic checks, not rendered-browser
acceptance.

**Next bounded check:** install on Pi 5 using the documented commands, then
verify normal HTTPS Apps access without forwarding, real login/logout/expiry,
closed-download authorization, reinstallation and web-only restart with the
same capture ID. Complete reboot/unit acceptance and broader browser coverage.
After that gate, proceed to milestone 5's same-source beacon pcapng export;
original packet bytes/timestamps/radiotap, mixed-frame analyzer input, disk
isolation and WiFi-Beacon-analysis context remain required. No release tag or
production Pi deployment was performed by this local implementation.

## Web/TUI alignment and Pi 4 compatibility — October 19, 2026

The user reports that the preceding protected-hosting test worked well on Pi 5.
Pi 4 installation refused its unrecognized WebUI application revision before
writing a card/route. All four supplied Pi 4 hashes exactly match upstream
`dc39a7e852113ff00f2654c005d588755ce753c9`. Review of its application against
the previous supported revision confirms identical login/freshness functions;
differences concern polling and the Grafana package check. The installer now
accepts that application hash while retaining exact auth-module/Nginx hashes
and AST gates. A read-only proposal against the exact upstream Pi 4 files
passes. Preflight and integration scripts are executable as well as usable
through `python3`. Installed Pi 4 routing/authentication acceptance is pending.

The web overview now follows [TUI organization](screens.md#web-organization):
channel composition first, all strongest-radio members and their QBSS marker,
then a separate selected-BSSID panel for QBSS, station maximum, retry maximum
and beacon-loss identity. AP/SSID/BSSID/RSSI context uses the existing screen
and analyzer selections, including distinct sources and strongest-radio cycling;
station details no longer borrow the QBSS selection. No metric is recomputed
by the browser. AP-name sorting, observed-active-client legend wording, PSC
labels, platform favicon links, matching Start/Stop session labels and six
current cards are implemented. The duplicate one-second BSSID card and local
survey web display/start option are removed. Survey engine/API/log semantics
remain intact; future verified reporting can add a distinct CU graph line.

Capture publications include the tuned channel definition and its verification
flag, allowing explicit center frequencies and MHz widths without assuming
requested tuning was applied. Page HTML embeds the current read-only cached
publication and renders existing session state/history before its first poll.
Missing-data, true-zero, graph-gap and `<1%` behavior is preserved.

Consumer opens now atomically use an existing managed owner regardless of the
requested source, with an already-running message and unchanged tuning/history.
The broker still rejects explicit conflicting starts. A plain CLI open attaches
to an active managed session; without one it retains foreground behavior, and
`--standalone` explicitly requests that behavior. Managed FPMS adds **Open
Running Session**, which cannot start a replacement, and displays the current
source when opened through another channel choice. Display-only opens and
exits preserve active shared logging. Source changes require Stop session and
an explicit new start. Update broker, CLI, web and FPMS together for these
development-publication additions.

**Evidence:** full Python 3.13.3 regression passed **741 tests with one optional
Core transport skip in 20.14 seconds**, including real broker/CLI attachment
and actual TLS Nginx route tests. Concurrent different-channel opens share one
owner, and CLI auto/explicit attach and managed opens retain the original
session/history. Tests cover current-page bootstrap, unavailable-broker recovery,
safe embedded JSON, capture tuning publication, FPMS resume/logging preservation
and unchanged auth gates. Exported synthetic fixtures and the checked-in
`tests/web_ui_check.mjs` pass simulated-DOM checks in protected and local modes
for distinct metric identities, radio membership, AP sorting, PSC labels,
center/width formatting, initial existing-session rendering, expiry/reconnect,
CSRF/session IDs and activity throttling. Native Brave access timed out, so
rendered-browser acceptance for this layout is not claimed.

**Next device checks:** repeat protected integration installation on Pi 4 and
open its normal HTTPS Apps/Utilization URL. Retest desktop/phone composition,
source identities, tuning labels and existing-session opens across web/CLI/FPMS
on both Pis. Finish installed login/logout/expiry/reboot and controlled busy-
channel checks before marking milestone 4 accepted. Beacon pcapng export and
WiFi-Beacon-analysis end-to-end context remain milestones 5–6; release metadata
stays `2.1.0.dev0`.

## Pi acceptance, FPMS callback repair and milestone 5a — October 20, 2026

The user confirms Pi 4 Apps access, login/logout/expiry and existing-session
attachment, plus web-page startup after reboot/relogin. Reboot acceptance means
the web/login route is available while capture starts idle. An in-memory capture
session is not resumed automatically. The page result is confirmed; a specific
post-reboot `idle` status was not supplied. Broader failure/download-authorization,
radio restoration and controlled busy-channel release checks remain open.

The user also reports the FPMS Stop Capture/Session item not stopping capture.
Inspection found that the native FPMS dispatcher executes only plain
`FunctionType` callbacks, while Stop Session and Open Running Session used bound
methods. Both actions now use plain wrappers consistent with Display/Logging.
Tests verify every managed menu leaf is dispatchable, stop passes the current
session ID and reports errors, and the actual pinned dispatcher gate reaches
the real synthetic broker. Stop ends the owner and logging with a cleanup
marker, preserves final history/session ID, and leaves the broker running.
Physical Pi retesting is still required after adapter reinstallation. The
expected stopped display retains final data with phase `stopped`, not a cleared
session; leaving a renderer remains detach-only.

**Milestone 5a complete locally:** `beacon_live/pcapng.py` adds a bounded block
reader, beacon selector and stream writer. It preserves complete selected
packet blocks verbatim, including payload/radiotap/FCS, timestamps and packet
options. All interface descriptions retain their original numbering and
timestamp resolution/offset metadata. Both section byte orders, multiple
sections/interfaces, Enhanced and obsolete Packet Blocks are covered. Changed
section lengths become unspecified; source statistics/secrets/unknown metadata
are omitted. Untimestamped, unsupported-link, truncated and malformed packets
have explicit skip counters; malformed containers/options, unsupported versions
and non-copyable custom options fail export. Default limits are 4 MiB per block
and 256 interface contexts per section.

`scripts/export_beacons.py` makes the foundation reviewable against existing
closed full-packet pcapng files. It never captures, retunes or controls a session.
It refuses existing destinations, writes a private temporary file, closes it
and atomically publishes without replacement, cleaning up owned partial files
on errors. Live capture/analysis, direct default, optional Core behavior,
CLI/FPMS metrics and shared history are unchanged by this export step.

**Evidence:** Python 3.13.3 full regression passes **813 tests with one optional
Core transport skip in 20.92 seconds**. New pcapng tests exercise exact bytes,
decimal/binary clocks, signed offsets, multi-interface/section contexts, opaque
extended radiotap/FCS, short reads/writes, malformed input, size/interface bounds,
write failure and publication races. Real TShark checks match source beacon
timestamps/interface IDs/radiotap/RSSI/SSID, including nanosecond timestamps.
A synthetic fan-out delivers all 27 original mixed frames to the existing
decoder/analyzer while selecting three beacon packets for export. Export on/off
snapshots match, including four eligible frames, three retries, one observed
client and 12 advertised stations in a completed second. This is a hardware-free
fan-out contract, not deployed live export or Pi throughput evidence.

**Next bounded implementation (5b):** connect a single full-packet source to
the existing decoder and a bounded export worker, keeping all required frames
on the analysis branch. Add independently readable rotation segments, owned-file
retention/free-space limits, disk/queue-failure isolation, publications/controls
and registered downloads. Confirm export on/off and slow/full-disk behavior
leave capture/history intact, then repeat physical direct/Core lifecycle checks.
The current offline helper is not a web download or live export toggle.
WiFi-Beacon-analysis validation with AP/classroom/schedule/timezone context is
still milestone 6. Grafana/touchscreen integration remain deferred and release
metadata remains `2.1.0.dev0`.

## FPMS refresh and session menu — October 21, 2026

The user confirms that the installed FPMS Stop Session action now works.
This accepts the reported menu action on the tested Pi; independent physical
radio-restoration evidence remains part of the broader acceptance checklist.
Testing also exposed repeated attachment popups, including after FPMS itself
started capture. Native FPMS reruns its current page action every refresh;
the adapter was querying status and presenting every invocation as a new join.

**Completed locally:** an existing display action now returns immediately,
reporting a finished child through the existing recovery path if needed.
Refreshes do not query the broker, reset handlers, change logging, relaunch the
renderer or repeat notifications. Joining an existing session shows one notice
per display connection. Starting from idle FPMS, or opening its live logging-only
proxy's session, does not show an attachment notice.

The managed menu now refreshes when entered with Right or Center. Idle menus
show band/channel choices. Active menus show session phase/current tuning,
Connect, Stop Session, Start Logging and Stop Logging. Connect is attach-only;
a session stopped elsewhere cannot be restarted by a stale Connect action.
Status failure shows Session unavailable rather than offering a new capture.
The adapter mutates its existing menu list before native FPMS enters it, keeping
native selection paths intact. Only the Utilization list triggers a service
check, and other applications retain their navigation. Standalone menus stay
unchanged. Menu status refreshes on entry, not continuously while left open;
reopen Utilization after another consumer changes a session.

**Evidence:** full Python 3.13.3 regression passes **819 tests with one optional
Core transport skip in 20.16 seconds**, including real broker/CLI lifecycle and
TLS Nginx authentication checks. New regression cases cover repeated managed
page actions after FPMS/external/logging-only starts, one attachment notice,
unchanged controls/handlers and one renderer. Both patched native entry buttons
exercise idle/starting/running/stopping/idle transitions and unchanged navigation
for another app. Native callback dispatch, real broker stop/log closure/history,
installer idempotence and rollback checks also pass.

**Device follow-up:** sync and reinstall with
`sudo ./scripts/install_wlanpi_fpms.sh --fpms-runtime managed --with-web`.
This restarts the broker and FPMS, ending any active capture. Check idle menu →
start Display → leave it open without popups → Left → reopen → Connect/Stop.
Then start through web/CLI, open FPMS Connect and confirm one attachment popup
and matching tuning/history. Reopen after stopping to verify band choices
return. Updated menu rendering still needs this hardware check. The next
implementation milestone remains 5b (live export fan-out, rotation and disk
limits); WiFi-Beacon-analysis context/validation remains milestone 6. Release
metadata stays `2.1.0.dev0`.

## FPMS session controls, logging notices and package-update recovery — October 21, 2026

Follow-up Pi testing found that Left returned to the original channel submenu,
where Stop Session was absent. The supplied configuration explicitly selects
managed runtime and direct capture; this report does not indicate standalone
capture. Managed Left continues to terminate only the renderer. A new native
page-return hook refreshes Utilization and returns directly to the current
session menu, making Connect and Stop Session available after a channel start.
Other applications retain their menu paths. Idle actions are renamed
Start Session and Start + Log. An absent FPMS runtime setting now defaults to
managed, while an explicit saved standalone selection is retained. Reinstallation
resolves the saved runtime and ensures its managed broker is installed, even
without an explicit runtime flag.

Start Logging and Start + Log inspect the owner's published logging status.
When active, they report **Logging already being saved**, preserve existing
file paths and avoid another enable request/background logging proxy. Starting
logging on an existing owner with logging inactive sends an ID-scoped request
without a new renderer, reporting Logging requested. Existing LoggingService
start behavior is also idempotent while active; the repeated start message was
not evidence of multiple analyzers or duplicated log writers.

CLI attachment information now has its own transient notice rather than using
the persistent Warning field. The interactive footer clears it after ten
seconds or any keypress, including while display updates are paused. Runtime
warnings retain their separate state and priority. Plain terminal rendering
also omits attachment information after its deadline on subsequent renders.

**Web recovery diagnosis:** the user reports FPMS **1.4.24-1** and WebUI
**1.21.17-1** after apt upgrades. All four supplied hashes match the reviewed
original WebUI files, including the supported `b90723ca…` application, and the
card/Nginx managed blocks are absent. No allowlist or auth change is needed for
this installed revision. Reapply `install_web_integration.py --check`, then
`install_web_integration.py`, separately, to restore the card and route without
restarting capture. The application installer's `--with-web` installs web
dependencies; its help/completion output now states the separate integration
step. Preflight reports card/route presence separately. The supplied evidence
is consistent with package replacement of modified files; successful route
restoration on this Pi is not yet reported. Different security-updated WebUI
revisions still require source/auth/routing review, preserving the existing
authentication boundary rather than bypassing revision checks.

**Evidence:** Python 3.13.3 full regression passes **829 tests with one optional
Core transport skip in 21.04 seconds**, including TLS Nginx authorization tests.
A new native FPMS Start + Log → Left → Stop Session regression uses the real
broker, owner, CLI renderer and LoggingService: Left preserves owner ID,
history and active logging paths, exposes the Stop Session menu, and repeating
Start Logging retains those paths. Stop then closes logs/owner while the broker
remains running. Other new cases cover active-logging messages without controls
or extra proxies, logging enable against the current owner, managed defaults,
transient CLI timeout/keypress/paused behavior without losing warnings/history,
and missing/partial managed web blocks. Installer shell syntax/help checks pass.

**Remaining device checks:** reinstall the application/FPMS adapter, check Left
and the returned menu on physical FPMS, repeat logging actions and verify paths,
and check CLI notice dismissal. Reapply protected web integration and verify
Apps, direct URL and login/logout/expiry after the package upgrade. Application
reinstallation restarts the broker and ends any active session; protected web
integration alone does not. The next implementation milestone remains 5b live
pcapng export/rotation/disk limits, followed by milestone 6 analysis-context
validation. Release metadata remains `2.1.0.dev0`.

## Combined web session control and default full installation — October 21, 2026

**Completed locally:** the web session bar now has one session button. It uses
Start session with the primary color when idle/stopped/failed, Stop session with
the danger color while starting/running, and disabled Stopping… during shutdown.
The idle action opens the existing setup dialog. The main Start logging/Stop
logging button remains, and the setup dialog retains its logging checkbox.
Start submission and controls are disabled while requests run, disconnected or
in incompatible phases; logging formats and current-session-ID checks remain.
No capture/analyzer or metrics behavior changes are introduced by these controls.

The default install/update command is now simply
`sudo ./scripts/install_wlanpi_fpms.sh`. It validates the protected platform
proposal first, installs application/CLI/FPMS and web dependencies, ensures the
managed broker, and applies the protected Apps card, route and web service.
Existing backend/runtime choices and the web proxy token are preserved. This
also repairs supported platform files replaced by an OS package upgrade.
No separate web-integration command is needed for standard installation.
`--with-web` remains compatible; `--without-web` explicitly skips web work and
retains any existing integration. The component web installer remains available
for read-only checks or web-only repair without restarting capture.

Unsupported-platform checks fail before application writes or capture restarts.
Later install failures propagate a nonzero exit instead of announcing a complete
install; the protected web component retains its own rollback behavior. The
full install restarts the broker and FPMS and ends an active session normally.
It updates the local checkout's code; source synchronization remains separate.

**Evidence:** full Python 3.13.3 regression passes **837 tests with one optional
Core transport skip in 23.64 seconds**, including real broker/CLI lifecycle and
TLS Nginx authorization. A new temporary-Pi shell harness exercises the actual
coordinator's defaults, saved standalone mode, legacy --with-web option, explicit
web opt-out, offline-wheel forwarding, early platform-check failure and later
web-apply failure. A protected-installer regression simulates package replacement
of the card and route and verifies restoration without duplicated blocks or
token replacement. Shell syntax/help checks pass.

The updated JavaScript simulated-DOM checks pass in protected and local modes:
one session button transitions label/color and shutdown state, current-ID stop
and logging requests retain CSRF, the logging button follows owner state,
the setup checkbox survives polls and reaches the start payload, cancel does
not start capture, and auth expiry prevents further mutations. Existing metric,
selection/table/graph, PSC and polling checks remain. These are frontend logic
checks, not rendered-browser or physical-Pi acceptance.

**Next Pi check:** sync the checkout, run the single standard installer, then
open Apps → Utilization through the normal login. Verify start-dialog logging,
main logging controls, combined session button and stopped-history retention.
Repeat the standard install after a supported WebUI update to verify automatic
card/route restoration. Milestone 5b live pcapng fan-out/rotation/disk limits and
milestone 6 WiFi-Beacon-analysis validation remain next; version stays
`2.1.0.dev0`.

## Dynamic FPMS menu and phase review — October 21, 2026

**Completed locally:** the active menu starts with Connect Session, followed by
Stop Session and exactly one logging action: Start Logging when inactive or
Stop Logging when active. The redundant running-status item is removed. Local
controls return through a refreshed root menu. While shutdown is in progress,
only Stopping Session is shown; band/channel choices return when the owner is
stopped, allowing a new explicit start without stale Connect/Logging actions.
Managed Stop Logging now reads the owner's actual state and sends an ID-scoped
request, including when another consumer started logging; it no longer reports
already stopped merely because FPMS has no local logging proxy.

An idempotent native FPMS menu-loop hook now consumes periodic status checks
while Utilization's menu is visible. A daemon worker reads through the installed
CLI, with at most one request in flight and at most one read every two seconds.
The worker never paints or changes FPMS navigation. The UI loop applies changes
and redraws only when the menu changes, so external web/CLI starts/stops and
logging changes normally appear within a few seconds. No checks run in other
apps, the parent Apps menu or the active metric display. Selection stays valid
when logging actions change; lifecycle changes collapse stale channel paths
to the correct root. Older pending reads cannot overwrite a local refresh.
Service failures show Session unavailable and recover on later reads.

The web Received clock now uses browser-local **24-hour HH:MM:SS** formatting.
Capture/log timestamps, metric definitions and publication history are unchanged.

**Evidence:** Python 3.13.3 full regression passes **847 tests with one optional
Core transport skip in 24.82 seconds**. Native installer/dispatcher fixtures cover
menu-loop redraw gating, entry/navigation and idempotent patching. Tests cover
external lifecycle/logging transitions, one bounded off-thread read, UI-only
menu application, polling scope, selection repair, stale-read rejection and
service recovery. The real broker/native-button regression now toggles logging
externally and observes the menu follow it without changing capture history;
Stop Session returns the menu to idle band choices. Protected/local JavaScript
simulated-DOM checks pass, including an en-US locale fixture that requires
Received 19:04:05, plus the existing control/auth/metric checks. Physical OLED
rendering and the latest asynchronous menu behavior still need Pi acceptance.

**Phase position:** milestone 1 is complete. Milestones 2–4 are implemented with
local regression coverage and the user-reported CLI/TUI, Pi 4/5 web, login,
logout/expiry, existing-session, reboot-page and Stop Session evidence recorded
above. The latest report says the current application looks good; it does not
close every controlled busy-channel, failure recovery, download authorization
or physical radio-restoration release check. These broader acceptance items
remain open rather than treating UI polish as completion of the release.

Milestone **5a is complete**: the bounded offline pcapng reader/filter/writer
and helper preserve original beacon packet blocks and pass TShark/fan-out
regressions. Milestone **5b is the next bounded implementation**: connect a
single full-packet source to the analyzer and beacon export worker, preserve all
required analysis frames, add readable rotated segments and retention/free-space
limits, isolate disk/queue failures, and expose status/registered downloads.
Then milestone **6** runs WiFi-Beacon-analysis with pinned AP/classroom/schedule,
date/timezone context and exported segments, closes release checks and prepares
v2.1.0. Grafana/touchscreen work remains deferred; metadata stays `2.1.0.dev0`.

**Next device check:** sync and run `sudo ./scripts/install_wlanpi_fpms.sh`
(this restarts services and ends an active capture). Leave Utilization's FPMS
menu open, start/stop the session and logging from web/CLI, and verify the menu
changes without reopening it. Confirm Connect Session, the single logging
action, Stop Session → idle choices, and Left detachment. Check the web receive
clock. Proceed with 5b after recording any resulting device corrections.
