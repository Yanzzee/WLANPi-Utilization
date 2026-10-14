# Core capture backend evaluation

**Decision accepted October 14, 2026:** keep direct capture as the default for
the standalone CLI and FPMS application. Retain Core capture as an explicit
optional backend for remote integration and future compatible stream sharing.
Successful Core acceptance tests do not automatically trigger a default change.

Core's [architecture](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/ARCHITECTURE.md)
centralizes device information and control for local consumers, including FPMS
and CLI tools, as well as remote consumers. Use those APIs where they simplify
platform integration. No requirement that every on-device analyzer use the
capture WebSocket was found in the reviewed documentation.

For capture, Core provides authenticated access, namespace resolution, a session
registry, owner-disconnect cleanup, and one stream with multiple consumers. The
current Utilization backend owns its capture and cannot subscribe to another
owner, so the stream-sharing benefit is not yet fully implemented. A compatible
shared stream must preserve our fixed-channel, single-source, unfiltered input.

Core 2.4.8 does not provide a system-wide physical-radio lease, saved-state
restoration transaction, or survey-counter API. The app still needs local radio
safety and restoration around Core capture. Core also adds service,
authentication and transport dependencies; their performance cost has not been
measured. Neither backend inherently improves adapter reception or analysis
accuracy. Reconsider the default only for a concrete sharing/integration need or
improved upstream radio lifecycle guarantees, with a separate decision and
device acceptance. Core expansion is separate from the direct-backend Trixie
release checks.

## Reviewed baseline

Reviewed October 13, 2026: WLAN-Pi/wlanpi-core default branch `dev`, commit
[`7e858ff40e9bc522aa881c479580362a3dd679e3`](https://github.com/WLAN-Pi/wlanpi-core/tree/7e858ff40e9bc522aa881c479580362a3dd679e3).
Its Debian changelog identifies `2.4.8-1`. The remote default HEAD matched
this revision on the review date; the device's installed version must be checked independently.
No matching 2.4.8/2.4.7 Git release tag was found during this review.

The [shipped capture contract](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/docs/capture-ws-mcp-handover.md)
is `wss://localhost:31415/api/v1/streaming/capture`, using the device certificate
at `/etc/nginx/ssl/self-signed-wlanpi.cert`. Authenticate with a JWT in the first
JSON message. An on-device service can obtain one through `getjwt`; no token
belongs in a URL, process argument, log, or diagnostic bundle.

The separate [REST capture guide](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/docs/api/wifi-capture-consumer-guide.md)
is explicitly marked **design, not implemented**. Its source IDs, session
control tokens, and summary endpoint are not the shipped contract.

## Fit and gaps

| Requirement | Current Core behavior | Utilization integration |
| --- | --- | --- |
| One capture, all available frame types | Core runs Dumpcap and streams PCAPNG; an empty `pcap_filter` adds no BPF filter. | Feed one stream to one local TShark decoder and the existing shared analyzer. No beacon-only filter. |
| One fixed channel | A one-entry channel list is applied at startup; it does not enter the repeated multi-channel hop loop. | Request exactly one frequency/width and verify actual tuning. |
| Multiple consumers | An authenticated owner controls its session; other authenticated connections can subscribe read-only. | Current backend is owner-only; future subscription must verify a compatible existing stream. |
| Source/namespace discovery | Capture accepts `wlanpiN`; Core resolves namespaces. `list_sessions` reports namespace/config. General layout is available through `/api/v1/network/config/status`. | Match the exact source and namespace. Existing arbitrary interface names cannot be passed unchanged. |
| Channel width and centers | Configuration accepts 20/40/80/160; center frequency is computed by Core. No explicit centers, 80+80, 320, or puncturing parameter. | Check requested definitions against Core's supported tuning contract. Preserve explicit unsupported errors. |
| Exclusive radio use | Core claims interface names in its capture manager. It does not claim the entire phy against every controller/tool. | Retain whole-radio safety checks; a Core session is not proof that sibling interfaces are idle. |
| Failed initial tuning | `CHANNEL_SET_FAILED` is emitted, but capture continues on the current channel. `CAPTURE_STARTED` contains requested configuration. | Abort that owned session on tune failure. Read back actual width/centers before presenting metrics as correctly tuned. |
| Exit/restoration | Owner stop/disconnect stops Dumpcap and channel tasks, releases session claims, and detaches subscribers. Prior tuning is not restored. | An owner must still ensure saved state is restored after Core stops; subscribers must never restore or retune someone else's radio. |
| Delivery/backpressure | Owner delivery is awaited; slow subscribers are closed with code 1013, with 1024-block queues and a four-subscriber cap. | Drain promptly, bound queues, and expose disconnect/drop gaps. Reconnection must not replay/count frames twice. |
| Local survey CU | PCAPNG does not supply `iw survey` counter deltas. | Continue read-only survey sampling in the correct namespace. Keep it separate from QBSS. |

These findings come from the shipped
[models](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/wlanpi_core/streaming/models.py)
and [connection manager](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/wlanpi_core/streaming/connection_manager.py).

### Confirmed channel-mapping gap

An isolated execution of Core's actual `_center_frequency` method produced:

| Primary MHz | Width MHz | Core result MHz | Expected center MHz |
| --- | --- | --- | --- |
| 2412 | 40 | -1 | 2422 for HT40+ |
| 5745 (149) | 80 | 5775 | 5775 |
| 5765 (153) | 80 | -1 | 5775 |
| 5785 (157) | 80 | -1 | 5775 |
| 5805 (161) | 80 | -1 | 5775 |
| 5975 (6 GHz 5) | 80 | 5985 | 5985 |

This is a source-level result, not a measurement on the Pi. Default 2.4 GHz
20 MHz and the tested 5 GHz channel 36 / 6 GHz channel 5 defaults are represented;
other primaries must not be assumed to work from width support alone. Correct
Core's upper-5-GHz range mappings and HT40 direction handling before claiming
parity with all current explicit tuning choices.

The supported-frequency query also matches integer `* N MHz` lines only. Our
device's earlier `iw phy` output uses decimal frequencies. Whether
`iw phy <phy> channels` has the same format on the device needs verification;
empty frequency results should be investigated rather than treated as proof
that no capture-capable radio exists. The user's October 13 probe returned a
populated 2.4/5/6 GHz frequency list for wlanpi0 on Core 2.4.8-1, so this
potential parsing gap did not affect that device's capture-frequency query.

## Single-radio and sharing policy

Core sharing could provide another option to exclusive independent captures:
one owner controls a fixed unfiltered capture, and Utilization subscribes to its
bytes. Multiple consumers could then analyze frames from one hardware capture.
Subscription remains future work; the current backend refuses an existing owner.

A candidate stream must have exactly one source, the requested frequency and
width, an explicitly empty filter, and one channel. A hopping, filtered, or
multi-source session is not compatible with the current metrics contract.
Metadata identifies candidates but is not proof of actual tuning or frame
delivery. Subscribe must recheck the returned configuration and actual source
state. If incompatible, report the conflict; do not stop/reconfigure the owner
or silently accept partial metrics.

As an owner, Utilization should retain radio snapshot/safety handling until
Core itself supplies a whole-phy preparation/restoration transaction. Starting
Core capture and preparing/restoring siblings are not currently one atomic API
operation. That race and restoration responsibility need an explicit design;
do not describe the present interface claim as a platform-wide radio lease.
As a subscriber, exit means detach; capture and radio ownership remain with the
existing owner.

## Decoder feasibility

The transport can be:

```text
Core Dumpcap → PCAPNG WebSocket → TShark stdin → existing parser/analyzer
                                             → shared displays/history/logs
```

A hardware-free experiment with TShark 4.6.0 emitted the first decoded packet
about 96 ms after writing its complete PCAPNG block, while stdin remained open.
Regression tests cover HT40/VHT160/HE80/EHT320, arbitrary 17-byte transport
chunks, preserved RSSI/QBSS, nine decoded frames, and three retries of four
eligible frames. All use the existing live parser and analyzer. Subsequent
TShark 4.4.19 tests and device runs confirmed decoding and normalization;
end-to-end latency and dropped-frame accounting still need device validation.
Current regression and device evidence is recorded below.

The raw stream can be written once for evidence while the same bytes feed the
decoder. A transport failure must stop the owned capture and mark incomplete
intervals explicitly. Network/pipe handles must not leak into aggregation
workers, and Core must finish stopping before local radio restoration.

## Metadata probe on the Pi

From the updated checkout:

```bash
dpkg-query -W wlanpi-core
sudo /opt/wlanpi-core/bin/python scripts/check_core_capture.py
```

This evaluation helper uses Core's existing `websockets` dependency. It obtains
an evaluation JWT through `getjwt`, holds it only in memory, authenticates,
lists sessions, and queries frequencies. It does not configure, start, stop,
subscribe to, or read a capture. Token issuance updates Core's token store;
`--token-file` can reuse an existing JWT or getjwt JSON instead. Do not paste
JWTs into chat or diagnostic output.

Defaults evaluate `wlanpi0`, 2412 MHz, 20 MHz. To assess another requested
configuration, pass `--interface`, `--frequency-mhz`, and `--width`. Output
labels matching sessions as **configured candidates** and leaves actual tuning
unverified. Empty session lists are normal when no Core client owns a capture.
The installer now installs its own pinned WebSocket client in the isolated app
environment. Borrowing Core's interpreter is still useful for this source probe;
normal installed Core capture uses `/opt/wlanpi-beacon-live/bin/python`.

In the development venv, run the transport checks with the Pi's TShark:

```bash
.venv/bin/python -m pytest -q tests/test_core_stream.py
```

## Optional Core development

These stages describe retained optional-backend work. They do not schedule a
default migration or block completion of the direct-backend Trixie release.

1. **Evaluation/device inventory:** run the metadata probe and decoder transport
   checks; confirm the installed Core contract and namespace/frequency behavior.
2. **Controlled owner prototype:** add an explicit Core backend for one fixed,
   unfiltered `wlanpiN` stream. Reuse the current analyzer and renderers. Test
   tuning failure, orderly stop/disconnect, cleanup, logging, and actual-channel
   verification for the optional backend.
3. **Parity and upstream gaps:** resolve radio preparation/restoration ownership,
   width/center limitations, mapping gaps, buffering/drop diagnostics, and the
   app venv dependency/credential lifecycle. Compare live/raw metrics on the Pi.
4. **Compatible subscription:** add opt-in subscription with verified source,
   namespace, fixed tuning, and empty filter; reject incompatible sessions and
   preserve the owner's state on exit.
5. **Reassess only when justified:** consider a default change for a concrete
   sharing/integration requirement or improved upstream radio lifecycle support.
   That requires a separate decision and device acceptance; passing the above
   checks alone keeps direct capture as the default. Core errors must not silently
   start direct capture.

FPMS defaults to direct capture; Core is now an explicit persistent opt-in. The
user reported testing item 8 changes; final publication checks remain in the
release checklist.

### Remaining optional-backend work

The owner prototype already delegates tuning and packet capture to Core and
feeds one PCAPNG stream to the existing decoder/analyzer. The remaining work
does not require replacing the analysis engine or redesigning the menu, and is
separate from direct-backend release acceptance.

1. **Client packaging: implemented, device deployment pending.** The app's
   `core` extra pins websockets 15.0.1, matching Core 2.4.8's dependency. The
   installer installs runtime wheels in the app venv before replacing app
   files, verifies importability in isolated Python, and supports offline wheels.
   Dependency failures leave the previous app files in place. Core's Python
   interpreter is no longer needed for installed CLI or FPMS Core capture.
   Certificate, missing dependency, endpoint and decoder errors are surfaced
   without credential output.
2. **Integrate installed CLI and FPMS launches.** Add an explicit opt-in Core
   setting to the existing FPMS launcher and test the installed app interpreter.
   The setting is implemented via `--fpms-backend core` on the installer, saved
   in `/etc/wlanpi-beacon-live/fpms.json`; reinstallation without a backend flag
   preserves it. Its update participates in FPMS patch/restart rollback. The
   service's `WLANPI_UTILIZATION_CAPTURE_BACKEND` override takes precedence.
   Exercise Display, Display + Log, background logging, navigation, rotation,
   menu exit and service shutdown. Preserve one analyzer/history throughout.
   Keep Core opt-in after device acceptance. Report Core errors instead of
   silently starting direct capture. Where it simplifies platform integration,
   extend the current authenticated layout read to actual-channel verification:
   `/api/v1/network/config/status` already includes each monitor's raw `channel`
   string with frequency, width and centers. Validate that data and namespace
   against the request, avoiding an extra local `iw` read where complete.
3. **Define the supported channel contract.** The pilot supports verified
   20/80 MHz captures on existing `wlanpiN` monitors. Resolve or explicitly
   exclude known 80 MHz mappings that Core 2.4.8 cannot represent. Add 40/160
   only after center/direction handling is verified; 80+80/320 and puncturing
   need Core API work for parity. Do not advertise unsupported configurations
   as full coverage or silently use direct tuning to bypass the API.
4. **Complete device and failure acceptance.** Test 5/6 GHz 80 MHz, including
   restore to a different original channel; repeated launches; startup/tuning
   failure; busy-radio conflicts; SIGTERM/FPMS exit; Core restart/disconnect;
   decoder failure/backpressure and adapter removal. Confirm local survey CU
   remains correctly scoped. Compare completed interior retry/QBSS intervals
   with the same saved packets and check raw/log byte/frame accounting.
   Define what recovery is possible after abrupt termination; the current
   parent cannot restore local radio state after it is forcibly killed.
5. **Document optional support after acceptance.** Record the tested Core/API
   version, widths, lifecycle behavior and limitations. Direct remains the
   normal CLI/FPMS backend; a future default change requires the separate
   reassessment described above.

**Sharing is a future feature and the strongest potential reason to expand
Core integration.** Add opt-in subscription only to a verified single-source, fixed,
unfiltered matching session. Subscribers detach on exit, do not prepare/restore
the radio, and do not stop the existing owner. This needs its own tests for
configuration changes, owner exit and slow-consumer detachment.

**Core packet capture and zero local radio access are different scopes.** The
shipped capture API has no whole-phy lease, saved-state restoration transaction,
or survey-counter API. Core's existing
[layout/status implementation](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/wlanpi_core/utils/network_config.py)
and [response schema](https://github.com/WLAN-Pi/wlanpi-core/blob/7e858ff40e9bc522aa881c479580362a3dd679e3/wlanpi_core/schemas/network/config_status.py)
can replace discovery and monitor-channel reads, subject to validated field
coverage. Its `wlan-link` endpoint describes managed associations/MLO links;
it is not a monitor-capture state contract.

Keep local radio reservation, preparation/restoration and survey handling around
the Core owner until those services exist upstream. Removing every local radio
operation needs a Core whole-phy reservation and temporary preparation/restore
transaction, adequate administrative-state/identity readback, survey counters
and recovery handling. Moving packet capture alone does not supply them.

### Test the installed Core path

Exit active Utilization captures before reinstalling. Run:

```bash
sudo ./scripts/install_wlanpi_fpms.sh --fpms-backend core
sudo cat /etc/wlanpi-beacon-live/fpms.json
/opt/wlanpi-beacon-live/bin/python -I -c \
  'import websockets; print(websockets.__version__, websockets.__file__)'
```

Expect version 15.0.1 from `/opt/wlanpi-beacon-live/lib/python3.13/site-packages`.
The source-copy installer still avoids package builds and system Python changes;
its new runtime wheel installation requires an index connection unless
`--wheel-dir DIRECTORY` is supplied with wheels matching the Pi's architecture.

First confirm the installed CLI, independently of the FPMS backend setting:

```bash
sudo wlanpi-beacon-live --capture-backend core --iface wlanpi0 \
  --band 2.4 --channel 1 --channel-width 20 \
  --log --log-dir /tmp/utilization-core-installed \
  --raw-pcapng /tmp/utilization-core-installed/channel1.pcapng \
  --core-diagnostics /tmp/utilization-core-installed/core-diagnostics.json
sudo cat /tmp/utilization-core-installed/core-diagnostics.json
```

Then test FPMS 2.4 GHz channel 1: Display, Display + Log, logging-only,
navigation, Key 2 rotation, Key 3 snapshot and return to menu. Verify one Core
session while running and no owned session after exit. Check the app selection:

```bash
sudo journalctl -u wlanpi-fpms --since "5 minutes ago" -n 150 --no-pager
```

Expect `Capture backend: core`. Core exit waits allow up to 90 seconds for
bounded stop/restoration before forced termination; normal cleanup returns
promptly. Service-stop/restart behavior still needs device acceptance.

Standard CLI commands without `--capture-backend` still use direct capture.
Changing the FPMS setting does not change that CLI default. To return FPMS to
direct capture, reinstall with `--fpms-backend direct`. Invalid settings are
rejected; Core failure never automatically launches direct capture. The debug
bundle records `fpms_backend.json`, `core_client.txt` and the service's effective
backend override. Host tests passed through a fresh copied app environment
with its own 15.0.1 client, real spawned transport/decoder and shared analyzer;
the installed FPMS path remains pending device testing.

## Controlled owner prototype on the Pi

The October 13 device report confirms Core 2.4.8-1, working JWT/WebSocket
authentication, tri-band frequencies on wlanpi0, and no active Core sessions.
The implemented owner backend uses a single fixed, unfiltered stream through Core.
Host validation passes 624 regression tests; the optional transport module was
also run from a fresh copied app venv with its own websockets 15.0.1 client,
passing three loopback end-to-end cases
with a real spawn worker/decoder, byte-for-byte raw preservation, and failed-tune
cleanup, including live publication through the real shared analyzer worker.
Worker startup re-imports the actual installed CLI entrypoint in that check;
the installed launchers now guard their main call against recursive startup.
Incremental stdin decoding and decoder signal isolation also pass with
the official TShark 4.4.19 macOS build; broader device acceptance remains pending.
The first owner test on wlanpi-bd2 verified 2412/20 tuning and an acknowledged
stop with no remaining Core sessions, but reported zero decoded rows and zero
normalized frames in the parent's last publication. The saved PCAPNG contained
2,866 radiotap packets, and device offline decoding succeeded with both basic
and production restricted-protocol settings. Core delivery is confirmed;
the initial decoding/analysis handoff failed acceptance. The worker previously
withheld progress when it had no usable metrics or beacons; progress now includes
rejected rows, so a zero parent count no longer hides parser failures.

The next device run reported 1,502 decoded rows, all rejected by normalization,
with Core stop and before/after radio restoration both verified. Saved-row
inspection traced rejection to `radiotap.flags.badfcs=False`: this diagnostic
FT_BOOLEAN field was incorrectly parsed as an integer. Multiple antenna signal
values sent every row through that full parser path. It now uses boolean
validation, accepting textual and numeric boolean representations. Regression
tests cover beacon, data and control frames without counting antenna signals
as additional frames. A synthetic three-antenna packet capture also passes with
TShark 4.4.19, retaining all nine frames, beacon metrics and 3/4 retries = 75%.
The subsequent 2.4 GHz channel 1 / 20 MHz device run passed: 328,160 bytes
received and forwarded, 899 decoded rows normalized to 899 frames, zero
malformed rows, no transport error, confirmed Core stop and verified radio
restoration. `wlanpi0` remained up on its saved no-HT channel and the managed
sibling retained its original down state. A later 5 GHz channel 40 / 20 MHz
run also passed: 215,532 bytes received and forwarded, 518 rows normalized to
518 frames, zero malformed rows, no transport error, and verified Core stop
and restoration. The saved before/after frequency was 2412 MHz because cleanup
returned the radio from the requested 5200 MHz capture to its prior no-HT
channel. The capture sidecar records tuning during capture; the diagnostics
before/after snapshots record the restoration baseline.

The user also reports that SSID display normalization works on the device.
Broader Core acceptance still requires bonded widths, 6 GHz, repeat start/stop,
startup failure, and saved-packet metric comparison described below.

Run from the updated repository using Core's Python environment. This is a
source-checkout prototype; the production app environment remains isolated and
FPMS uses whichever backend its configuration selects. This source-checkout
wrapper can still use Core's existing client dependency; the installed path
above is the next device acceptance target.

1. Exit Utilization in FPMS and release the radio in any other capture tool.
   Record the current interface and sibling state:

   ```bash
   iw dev
   ip -j link show dev wlanpi0
   iw dev wlanpi0 info
   ```

   Also record each sibling's administrative state. For a namespaced source,
   run the inspections in that namespace and pass `--netns NAME` below.
2. Start with 2.4 GHz channel 1, 20 MHz, and let it run for at least 60 seconds:

   ```bash
   sudo /opt/wlanpi-core/bin/python scripts/run_core_capture.py \
     --iface wlanpi0 --band 2.4 --channel 1 --channel-width 20 \
     --log --log-dir /tmp/utilization-core-24 \
     --raw-pcapng /tmp/utilization-core-24/channel1.pcapng \
     --core-diagnostics /tmp/utilization-core-24/core-diagnostics.json
   ```

   The wrapper selects `--capture-backend core`. It checks dependency, capability,
   mode, Core namespace/layout, sessions, and frequencies before preparation.
   Core performs the tune and Dumpcap capture. Local TShark runs `-r -` as a
   decoder, and the existing analyzer supplies all metrics/history/renderers.
   JWT and socket state stay in a separate spawn worker, avoiding inheritance
   by the analysis worker. The wrapper now saves a radio baseline automatically
   before preparation, updates transport/analyzer counters while running, and
   writes a readback comparison after radio cleanup. Without an explicit path,
   it uses `/tmp/wlanpi-utilization-core-PID.json` and prints its location on exit.
3. While it runs, check Core's registry from a second terminal:

   ```bash
   sudo /opt/wlanpi-core/bin/python scripts/check_core_capture.py
   ```

   Expect exactly one new session owned by `wlanpi-utilization-owner`, one
   wlanpi0 source, one channel at 2412/20, and an empty filter. Confirm normal
   live metrics, display navigation, logging, and usable survey data if enabled.
4. Stop with Ctrl-C. Run the metadata probe again: the owned session must be
   gone. Repeat the baseline interface checks; saved administrative state and
   known monitor tuning must be restored. The radio guard rechecks ownership
   before restoring; if a new tool took the radio or Core did not release it,
   restoration must fail explicitly rather than retuning that owner's capture.
   The wrapper prints byte/row/frame counts, stop acknowledgement, and radio
   verification after the terminal UI closes. Inspect the full report:

   ```bash
   sudo cat /tmp/utilization-core-24/core-diagnostics.json
   ```

   `restoration_verified: true` compares saved identity, interface mode and
   administrative UP flags for all siblings, and known monitor frequency,
   width, centers and no-HT state. It does not infer a previous channel when the
   baseline had none. An empty Core registry proves that no registered capture
   remains; it does not prove restoration by itself. An existing `wlanpi0`
   monitor interface should remain a monitor interface after cleanup.
5. Preserve the raw PCAPNG, CSV, JSONL, and `.pcapng.json` sidecar. The sidecar
   must show `capture_backend: core`, `core_role: owner`, a session ID,
   `core_stop_confirmed: true`, verified actual tuning, and no transport error.
   Compare completed interior seconds with the offline retry audit, using the
   same saved packets. Core does not expose live drop counters; an unavailable
   drop count is not zero.
6. After the first run passes, repeat 5 GHz channel 36 and 6 GHz channel 5 with
   separate directories/filenames. Omit `--channel-width 20` to request the
   default 80 MHz block on those bands. Use `--local-cu --survey-debug` for local
   survey checks. This prototype supports 20/80 only; 40/160/80+80/320 and
   punctured definitions remain outside this iteration. Known Core 80 MHz
   mapping gaps (such as channel 157) produce an error before mutation; choose
   20 MHz explicitly to test such a primary.
7. Exercise a repeated start/stop and a startup failure. Verify errors stop the
   owned session and restore state. An existing Core session is refused in this
   iteration; subscribers are the next stage. Capture a failure promptly with:

   ```bash
   sudo journalctl -u wlanpi-core --since "5 minutes ago" -n 150 --no-pager
   sudo ./scripts/pi_collect_debug.sh --since "5 minutes ago"
   ```

   CLI stderr is separate from the FPMS journal. Retain it with the bundle.

The transport drains complete byte chunks during orderly stop, bounds stalled
pipe writes, and reports unexpected end/tune failure rather than continuing on
an unverified channel. Whole-phy preparation/restoration is still a local
transaction around Core's interface-level ownership, so the cross-tool race
described above remains a limitation until the platform offers a radio lease.

### Diagnosing an empty display

Use the same report from a second terminal while capture runs. `received_bytes`
counts WebSocket binary bytes (including PCAPNG headers, not just packets);
`forwarded_bytes` counts completed writes to the decoder. `decoded_records` and
`normalized_frames` come from the existing shared analysis path. Counts in the
report describe the latest publication observed by the parent; the final
buffered tail can differ from an offline file count. No additional live reader
or capture is created.

After stopping, inspect the preserved stream without using the radio:

```bash
ls -lh /tmp/utilization-core-24/channel1.pcapng
capinfos -c -E /tmp/utilization-core-24/channel1.pcapng
tshark -r /tmp/utilization-core-24/channel1.pcapng -Y wlan \
  -T fields -e frame.number -e wlan.fc.type -e wlan.fc.subtype | head -n 10
sudo journalctl -u wlanpi-core --since "15 minutes ago" -n 100 --no-pager
```

Zero received bytes points to the Core/Dumpcap delivery path. Nonzero bytes
alone may contain only headers, so check the completed file's packet count.
Packets with no `wlan` rows require checking encapsulation/decoding. Offline
WLAN rows with zero live rows narrow the issue to the stream decoder or its
analysis handoff. Missing QBSS fields do not imply that no packets were captured.
Rejected rows now publish progress even without usable metrics, without
finalizing any retry bucket on a wall-clock deadline.
The local decoder now runs in its own process session so foreground Ctrl-C
reaches the parent, which stops Core before closing the decoder and restoring
the radio. Transport status is drained even when no decoded rows arrive.
