# Architecture

This document is the technical source of truth for WLANPi Beacon Live. It
describes the current Phase 1–5 architecture and the constraints future work
must preserve.

User operation belongs in the [README](../README.md). Detailed display and log
behavior lives in [screens.md](screens.md) and [logging.md](logging.md).

## Core invariants

- Capture each decoded 802.11 frame exactly once.
- Analyze each frame through one shared analyzer pipeline.
- Keep a continuous 120-second rolling history.
- Publish immutable snapshots for every renderer and shared completed
  statistics for logging.
- Never restart capture, analysis, or history when the active screen changes or
  logging starts/stops.
- Keep UI state, logging state, and analyzer state separate.
- Keep hardware-specific code isolated from parser and analyzer logic.
- Keep the live path free of pandas.
- Users never select an SSID or BSSID.
- Treat AP QBSS utilization and local survey utilization as separate metrics.
- Treat advertised QBSS station count and locally observed unique client-MAC
  count as separate metrics. Do not call observed clients an associated-station
  count.

## Data flow

```text
monitor interface / replay input
            │
            ▼
 acquisition and parsing ── one normalized FrameRecord per decoded frame
            │                 └── beacon channel-definition observations
            │                              │
            │                              ▼
            │                    capability/coverage planner ── in-place iw tune
            │
            ▼
      shared Analyzer ───── rolling frames, latest beacons, derived metrics
            │
            ▼
 shared analyzer output
      ├── immutable MetricsSnapshot ── terminal/LCD renderers
      └── completed SecondStats ────── shared logging service

 valid beacon projections ─────────── shared logging service
```

Display navigation changes only the active screen index. Logging changes only
whether the logging service consumes records and snapshots. Neither operation
creates another analyzer or capture worker.

## Acquisition layer

Live capture configures one interface in monitor mode and launches one
line-buffered TShark process. Automatic width selection uses a fixed 20 MHz
definition on 2.4 GHz and the standard 80 MHz block containing the selected
primary on 5 GHz and 6 GHz. This reflects the supported WLAN Pi USB-adapter
capture limit. If the default 80 MHz definition is unsupported or rejected, the
hardware boundary warns and retries once at 20 MHz. Explicit CLI width/center
definitions are validated and are never silently changed.

Supported HT, VHT, HE 6 GHz, and EHT operation fields still form immutable
per-BSSID `ChannelDefinition` values. They are used to scope retry metrics to
the selected primary and to report incomplete fixed-width coverage. Beacon
contents never cause a retune, channel hop, capture restart, analyzer restart,
or history reset.

The live TShark process enables only the Radiotap/802.11 dissector chain and
disables WLAN decryption and defragmentation. The analyzer still receives every
decoded 802.11 frame; retry eligibility then excludes control and extension
frames without a separate capture pipeline. None of the metrics requires
payload or higher-layer protocol dissection. A 16 MiB capture buffer provides
headroom during short scheduler stalls. Normal live capture uses a 1024-byte
snapshot length and warns once per BSSID when a larger beacon is truncated and
later information elements may be unavailable. Interactive terminal renderers
show that warning inside their managed screen instead of writing beneath it.

Optional `--raw-pcapng` adds `-P -w` and restores full-length `-s 0` capture on
that same TShark process. It therefore saves the raw packets feeding live
decoding without a second competing capture.
The adjacent JSON sidecar records requested/actual definitions, verification and
coverage status, negotiated fields, full-snapshot policy, decoded/normalized
counts, and a drop count when TShark reports one. PCAPNG interface statistics
remain the authoritative source when TShark does not expose a drop count.

Successful channel tuning does not prove that the adapter can demodulate and
deliver every PHY/frame combination within that definition. Hardware testing
found an MT7921U configuration that tuned 80 MHz in 6 GHz but did not deliver
payload-bearing data frames, although Null/QoS Null frames were present and a
different capture device saw QoS Data. The application cannot reconstruct
frames absent from the kernel capture stream; raw-PCAPNG comparison is the
boundary test for this class of limitation.

The parser normalizes available fields into `FrameRecord`, including:

- capture timestamp;
- 802.11 type, subtype, and readable Retry bit;
- BSSID, TA, RA, SA, and DA addresses;
- SSID and RSSI;
- beacon interval (and frame length when present in older replay exports);
- QBSS channel utilization, station count, and admission capacity; and
- supported vendor AP-name/vendor clues;
- advertised primary, width, centers, completeness/ambiguity, and EHT
  puncturing when exposed; and
- optional PHY bandwidth, captured/original lengths, FCS, sequence, fragment,
  QoS TID, and A-MPDU reference diagnostics.

TShark fields are negotiated from `tshark -G fields`, retaining compatibility
with older installations. The export uses `occurrence=a` with a fixed
aggregator. Drivers normally supply one capture record per MPDU; if a dissector
does expose multiple WLAN headers in one record, the parser emits each aligned
MPDU and Retry bit rather than retaining only the first occurrence.

A valid beacon can also be represented as `BeaconRecord`. Replay inputs feed
the same aggregation behavior without requiring Wi-Fi hardware.

Hardware commands (`ip`, `iw`, TShark, survey reads) stay in the live/survey
boundary. Parsers, aggregation, selection, grouping, and renderers remain
hardware-testable.

## Analysis layer

`Analyzer` owns the rolling state and publishes `MetricsSnapshot` objects. It
maintains:

- normalized frames in the current 120-second window;
- beacons grouped by BSSID;
- the latest authoritative beacon for each BSSID;
- per-second history rows;
- per-BSSID and channel retry counts;
- per-second and rolling-window unique client-MAC sets;
- automatically selected display BSSIDs;
- best-effort radio groups;
- per-second strongest-radio beacon loss; and
- channel-composition state.

The analyzer ingests live rows continuously but avoids rebuilding a snapshot
for every busy-channel frame. It publishes a second after ordered capture time
passes that boundary by the 102.4 ms beacon-delay allowance, and when pending
data is flushed during shutdown.

Frame association, retry eligibility, and client detection are projected once
per capture second. Completed one-second projections are retained for the
rolling window and merged for snapshot publication. If the retained BSSID set
changes, affected projections are rebuilt from the single raw-frame store so
address-based association keeps the same meaning. The completed projection is
also reused when publishing that second, avoiding a second window scan.

Target-primary association uses timestamp-indexed beacon timelines and binary
search instead of scanning every retained beacon for every frame. Projection
cache keys contain only the channel definitions that can affect that capture
second, so a later beacon does not invalidate and recompute the preceding
two-minute history.

When live capture supplies a selected primary frequency, retry projection is
temporally scoped. A frame must associate through BSSID/TA/RA/SA/DA with a
BSSID whose channel definition was observed at or before that frame, remains
fresh, and advertises that primary. Pre-discovery frames, unknown associations,
stale definitions, and BSSIDs whose own primary is merely inside a secondary
portion of the bonded capture are excluded from retry readable/eligible/retry
counts. Replay without channel-definition fields retains its legacy behavior.

### Rolling-window and latest-beacon rules

- A BSSID is present only while at least one of its beacons remains in the
  rolling window.
- For an already selected BSSID, its newest beacon in the window is
  authoritative for SSID, QBSS utilization, station count, admission capacity,
  AP name, vendor, and beacon interval.
- Older beacon contents never overwrite newer values.
- The latest-beacon rule chooses values within a BSSID; it is not a BSSID
  selection tie-breaker.
- Inactive screens continue receiving the same new history through shared
  snapshots.

### QBSS BSSID selection

Only BSSIDs whose latest retained beacon contains usable QBSS channel
utilization are candidates for the Utilization and Admission screens.

1. Find the strongest peak RSSI in the window.
2. Treat candidates within 3 dB of that signal as near-equal.
3. Prefer the near-equal candidate advertising the highest latest station
   count.
4. If the current selection remains tied, keep it to prevent churn.
5. Use a deterministic BSSID ordering only after those rules; never use beacon
   or frame timing as a tie-breaker.

Users cannot override this selection.

### Retry buckets and selection

Retry samples are independent one-second channel ratios. A live second closes
only after an ordered capture timestamp passes the boundary plus the 102.4 ms
beacon-delay allowance; a wall-clock UI refresh must not finalize it while
older TShark rows may still be buffered.

The denominator contains frames eligible for Retry-bit retransmission:
unicast data plus retry-capable unicast management frames with a readable Retry
bit. Exclude beacons, probe requests, Action No Ack, group-addressed frames,
control frames, extension frames, and frames without a readable Retry bit.
Count every captured retry transmission, including multiple retries of the same
original frame.

A frame is associated with a known BSSID when that BSSID appears in `bssid`,
TA, RA, SA, or DA. The retry footer uses the BSSID with the highest one-second
retry percentage. It keeps the current footer on a percentage tie and falls
back to the strongest beacon RSSI when no retries occurred. Timing is never a
tie-breaker.

### Unique client-MAC counts

Client-MAC detection uses only data frames associated with a retained,
on-channel beacon BSSID. The candidate client is the unicast TA/RA link
endpoint opposite that BSSID. Known BSSID addresses, group addresses,
management/control/extension frames, unlinked data frames, and SA/DA-only
addresses are excluded. This prevents authentication and probe traffic, other
APs, and hosts behind the distribution system from being presented as observed
wireless clients.

Each completed `SecondStats` contains the deduplicated count for that capture
second. `MetricsSnapshot` additionally contains the deduplicated union across
the retained 120-second frame window for the Stations summary. Retry copies of
a frame do not inflate either unique count.

### Radio grouping

Radio grouping is deliberately conservative and best-effort. A BSSID is not
assumed to equal a physical radio.

Current grouping requires a shared vendor OUI and compatible AP-name/RSSI/MAC
evidence. Matching AP names permit a wider RSSI tolerance; without an AP name,
BSSIDs need similar RSSI and related MAC addresses. Conflicting known AP names
prevent grouping. Complete-link compatibility prevents a chain of weak matches
from collapsing clearly different radios.

AP names and vendor-specific fields are clues, not standardized identities.
Composition counts and Beacon Loss screen radio membership must therefore be
treated as estimates.

Rolling BSSID state remains available for the full two-minute window, but a
BSSID is eligible for strongest-radio selection only when its latest beacon is
no more than 1.1024 seconds old. That covers one complete reporting second plus
the delayed-beacon allowance: a genuinely missed second can still be measured,
while an old high-RSSI observation cannot hold Beacon Loss at zero after that
radio stops being heard.

## Snapshot model

The immutable snapshot contains:

- the generated/reference time and window length;
- current and historical `SecondStats`;
- current `BssidState` and `RetryBssidState` collections;
- selected QBSS, station, and retry BSSIDs;
- rolling unique client-MAC counts for the channel and for each retained
  BSSID, plus the analyzer-selected top count and its `qbss` or `mac` source;
  and
- `BeaconReceptionSnapshot` and `CompositionSnapshot` views of the selected
  strongest radio.

UI code reads the snapshot and formats a view. It must not independently parse
frames, select BSSIDs, or compute metrics.

## Display layer

`ScreenManager` owns only the active screen index and navigation debounce. The
six screen definitions are pure renderers over a snapshot:

1. Utilization
2. Admission
3. Stations
4. Retries
5. Beacon Loss
6. Composition

Up/down navigation changes the index with wraparound and a 0.2-second debounce.
It does not reset analyzer state. The LCD renderer writes a PPM frame and JSON
display state; the thin FPMS adapter owns GPIO/menu integration and draws those
artifacts on the device display. While the display page is active, the adapter
overrides all three FPMS auxiliary buttons: the first two are no-ops and the
third atomically saves a PNG copy of the current composed screen in the FPMS
log directory. Screenshot capture does not change the active renderer or
analyzer state.

The Stations renderer overlays the advertised QBSS sum and per-second unique
client-MAC series from that snapshot. Its `TOP` identity and count are also
selected by the analyzer, with QBSS preferred over MAC discovery on an equal
count. The renderer does not inspect frames itself.

See [screens.md](screens.md) for exact metric and presentation behavior.

## Logging layer

One `LoggingService` is shared by display-with-logging and logging-only modes.
It consumes valid beacon projections and completed analyzer statistics, and it
owns file state, disk checks, low-disk markers, and rollover. Logging-only mode
substitutes a no-op renderer but runs the same acquisition and analyzer.

Log records enter a bounded FIFO queue and one logging thread owns record
serialization, writes, and per-record flushes. Rollover, stop, and low-disk
handling drain and join that worker before closing or replacing its files.
Queue saturation applies backpressure rather than dropping records.

Logging state can change without changing the analyzer or capture process. If
disk pressure stops logging during display mode, display capture continues. If
it occurs in logging-only mode, the process exits cleanly after closing logs.

The analyzer remains single-threaded by design because capture timestamp order,
latest-beacon authority, selection hysteresis, and immutable one-second closure
share one state transition. TShark already performs capture/dissection in a
separate process, while the logging worker removes disk I/O from that ordered
analyzer path. Per-frame multiprocessing is avoided because serialization and
reordering costs would work against the lightweight normalized row format.

See [logging.md](logging.md) for formats and policy.

## Runtime modes

- **FPMS display:** live capture plus LCD artifacts and screen navigation.
- **FPMS display and log:** the same runtime with logging active.
- **FPMS logging-only:** the same capture/analyzer with rendering disabled.
- **Terminal live:** the same analyzer with a terminal renderer.
- **Replay:** saved TSV input through parser/aggregation behavior without Wi-Fi
  hardware.
- **Retry debug:** offline PCAP/PCAPNG audit with explicit exclusion counts.

The FPMS adapter prevents simultaneous display and logging-only capture
sessions so the device never starts two capture pipelines for one app session.

## Module boundaries

| Area | Main modules |
| --- | --- |
| Models and snapshots | `beacon_live/models.py` |
| Parsing | `beacon_live/parser.py` |
| Rolling analysis and selection | `beacon_live/analyzer.py`, `beacon_live/aggregator.py` |
| Radio grouping | `beacon_live/radio_grouping.py` |
| Screens and UI state | `beacon_live/screens.py`, `beacon_live/screen_manager.py` |
| Terminal/LCD rendering | `beacon_live/dashboard.py`, `beacon_live/lcd_dashboard.py` |
| Live hardware boundary | `beacon_live/live.py`, `beacon_live/survey.py` |
| Logging | `beacon_live/log_writer.py` |
| CLI/device entrypoints | `beacon_live/cli.py`, `beacon_live/device.py` |
| FPMS integration | `integration/wlanpi_fpms/channel_utilization.py` |

## Development rules

- Prefer dataclasses, pure functions, immutable snapshots, and small modules.
- Add pytest coverage for parser, aggregation, selection, grouping, logging,
  and navigation behavior.
- A new display screen should normally be a new renderer over existing shared
  state—not a new analysis path.
- Do not introduce hardware requirements into unit-testable analysis code.
- Preserve laptop replay mode and the common engine used by live, replay,
  logging, and display workflows.

See [development.md](development.md) for setup and test commands.

## Phase history

The phase structure remains the project roadmap and implementation history.

### Phase 1 — shared rolling snapshot

- Introduced the single analyzer and 120-second state.
- Kept the original utilization workflow through shared snapshots.
- Established newest-beacon authority within each BSSID.

### Phase 2 — screen navigation and core metrics

- Added navigation without capture/history resets.
- Added admission-capacity and total-station-count renderers.

### Phase 3 — retry percentage

- Expanded the canonical live input to all decoded WLAN frames.
- Added capture-timestamp-driven one-second retry buckets and offline audit.

### Phase 4 — composition and radio grouping

- Added conservative AP-name/OUI/RSSI/MAC radio grouping.
- Added the channel-composition renderer.

### Phase 5 — logging safety and documentation

- Added shared toggleable logging for display and logging-only operation.
- Added periodic free-space checks, low-disk markers, and hourly rollover.
- Split user, screen, logging, troubleshooting, testing, and development
  documentation by audience.
