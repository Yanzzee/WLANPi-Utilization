# Core capture backend evaluation

Core's authenticated capture WebSocket is the platform integration point to
use for a shared capture backend. Utilization should move toward a Core-owned,
single-source, fixed-channel capture. The current API can carry the frames we
need, but transport delegation alone does not preserve all of the current radio
ownership/restoration guarantees. Address those gaps before changing the FPMS
default.

## Reviewed baseline

Reviewed October 13, 2026: WLAN-Pi/wlanpi-core default branch `dev`, commit
[`7e858ff40e9bc522aa881c479580362a3dd679e3`](https://github.com/WLAN-Pi/wlanpi-core/tree/7e858ff40e9bc522aa881c479580362a3dd679e3).
Its Debian changelog identifies `2.4.8-1`. The remote default HEAD still matches
this revision; the device's installed version must be checked independently.
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
| Multiple consumers | An authenticated owner controls its session; other authenticated connections can subscribe read-only. | Share only a compatible existing stream, or own a new capture. |
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

Core sharing provides another option to exclusive independent captures: one
owner controls a fixed unfiltered capture, and Utilization subscribes to its
bytes. Multiple consumers can then analyze frames from one hardware capture.

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
eligible frames. All use the existing live parser and analyzer. TShark 4.4
stdin behavior and real Core/Dumpcap latency remain device checks. The full
Python 3.13 suite passes all 507 tests, including metadata-only protocol and
incremental decoder tests.

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
The actual production backend must install its own compatible WebSocket client
in the isolated app environment; borrowing Core's interpreter is for this probe.

In the development venv, run the transport checks with the Pi's TShark:

```bash
.venv/bin/python -m pytest -q tests/test_core_stream.py
```

## Iterative migration

1. **Evaluation/device inventory:** run the metadata probe and decoder transport
   checks; confirm the installed Core contract and namespace/frequency behavior.
2. **Controlled owner prototype:** add an explicit Core backend for one fixed,
   unfiltered `wlanpiN` stream. Reuse the current analyzer and renderers. Test
   tuning failure, orderly stop/disconnect, cleanup, logging, and actual-channel
   verification before making it the FPMS default.
3. **Parity and upstream gaps:** resolve radio preparation/restoration ownership,
   width/center limitations, mapping gaps, buffering/drop diagnostics, and the
   app venv dependency/credential lifecycle. Compare live/raw metrics on the Pi.
4. **Compatible subscription:** add opt-in subscription with verified source,
   namespace, fixed tuning, and empty filter; reject incompatible sessions and
   preserve the owner's state on exit.
5. **Default migration:** select Core for supported configurations once the
   acceptance checks pass. Keep any direct fallback explicit so its independent
   radio ownership and limitations remain visible.

FPMS still uses direct capture while the Core-owner prototype is tested. The
user reported testing item 8 changes; final publication checks remain in the
release checklist.

## Controlled owner prototype on the Pi

The October 13 device report confirms Core 2.4.8-1, working JWT/WebSocket
authentication, tri-band frequencies on wlanpi0, and no active Core sessions.
The next iteration owns a single fixed, unfiltered stream through Core.
Host validation passes 536 regression tests; the optional transport module was
also run separately with websockets 17.1, passing two loopback end-to-end cases
with a real spawn worker/decoder, byte-for-byte raw preservation, and failed-tune
cleanup. The live device owner path remains pending.

Run from the updated repository using Core's Python environment. This is a
source-checkout prototype; the production app environment remains isolated and
FPMS continues to use its tested backend. No new packages need installation on
the Pi because Core already has the WebSocket client dependency used by the
successful metadata probe.

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
     --raw-pcapng /tmp/utilization-core-24/channel1.pcapng
   ```

   The wrapper selects `--capture-backend core`. It checks dependency, capability,
   mode, Core namespace/layout, sessions, and frequencies before preparation.
   Core performs the tune and Dumpcap capture. Local TShark runs `-r -` as a
   decoder, and the existing analyzer supplies all metrics/history/renderers.
   JWT and socket state stay in a separate spawn worker, avoiding inheritance
   by the analysis worker.
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
