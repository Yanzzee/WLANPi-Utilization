# Beacon pcapng export

Managed web/CLI/FPMS sessions now support beacon-only pcapng export from the
same packet source used for live analysis. Direct remains the default; Core is
optional. Export starts only when requested. Starting/stopping export does not
restart capture, retune the radio, replace the analyzer or reset history.

## Live controls and downloads

After syncing, install/update with `sudo ./scripts/install_wlanpi_fpms.sh`
(the installer restarts services and ends an active session). Open the protected
`/utilization/` page, start a new session and select **Start beacon pcapng
export**, or use **Start beacon export** beside the logging button at the top
or under **Log files** during the session. Both controls stay synchronized with
web/CLI/FPMS export changes and disable together when stopping or disconnected.
Stop export to drain accepted packets and flush/close the current segment; analysis continues.
CSV/JSONL logging and beacon export have independent controls.

In managed FPMS, a channel submenu offers **Start Export** to start a session
with beacon export and CSV/JSONL logging off. During a session, the root offers
only **Start Export** or **Stop Export**, independently of Start/Stop Logging.
These controls target the current session ID and return to the refreshed menu.
The existing two-second background status check also follows web/CLI changes.
Stop Export remains visible while accepted packets drain and the file closes.
Legacy standalone FPMS menus retain their existing controls.

A managed CLI/FPMS start configures the full-packet source even when export is
off, allowing a later web/CLI export request. CLI examples:

```bash
sudo wlanpi-utilization --managed --band 2.4 --channel 6 --start-only --beacon-pcapng
sudo wlanpi-utilization session status --summary
session_id="$(sudo wlanpi-utilization session status --summary | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')"
sudo wlanpi-utilization session export --session-id "$session_id" --enabled no --summary
sudo wlanpi-utilization session export --session-id "$session_id" --enabled yes --summary
```

The variable reads the actual current ID; do not pass the literal `SESSION_ID`.
Mutating controls reject stale IDs. Legacy standalone capture also supports
`--beacon-pcapng`, but has no managed mid-session controls. `--raw-pcapng` keeps
its existing diagnostic path and opts out of managed beacon export for that
session. The two export modes cannot be combined.

Default files live under `/var/log/wlanpi-beacon-live/beacons/` for web/FPMS
sessions, or `<log-dir>/beacons/` for CLI starts. Filenames use
`utilization_beacons_<writer-uuid>_<sequence>.pcapng`. The web lists registered
closed segments under its configured log root and offers authenticated binary
downloads with the same identity/symlink/race checks as CSV/JSONL. Active
`.pcapng.partial` files are excluded. Retention can remove a file before it is
opened (reported unavailable/404); an already opened descriptor remains usable.
The broker catalog remains bounded and in memory, like existing CSV/JSONL
inventory; a broker restart does not reconstruct historical download entries.
Completed files remain on disk unless manually removed or an explicit opt-in retention policy removes them.

## Rotation, retention and storage failure

| Setting | Default | CLI option |
| --- | --- | --- |
| Segment size | 256 MiB | `--beacon-segment-mb` |
| Segment duration | 3600 seconds (one hour) | `--beacon-rotation-seconds` |
| Retained bytes, including active segment | Unlimited (0) | `--beacon-retained-mb` |
| Retained files, including active segment | Unlimited (0) | `--beacon-retained-files` |
| Free-space reserve | 256 MiB | `--min-free-mb` (also applies to CSV/JSONL) |
| Pending export bytes / records | 8 MiB / 64 records | Python `ExportLimits` |

Rotation uses monotonic elapsed time and exact output size, never packet clock
rounding. Time rotation also closes a quiet active file. A new file opens only
when a beacon arrives. Every segment begins with its original section/interface
context and can be decoded independently. A section or interface-context change
also closes the preceding segment. A beacon plus context exceeding the segment
limit fails export explicitly rather than producing an oversized file.

There is no default file-count or total-output-size limit, and no automatic
deletion of completed captures. Export stops at the free-space reserve, closes
valid buffered output and leaves existing files intact while analysis continues.
For operators who explicitly need a ring buffer, positive `--beacon-retained-mb`
or `--beacon-retained-files` values enable the existing oldest-owned-file deletion
policy; zero disables each cap. Unrelated names, symlinks and foreign owners are
never candidates. Do not store unrelated files under the reserved export naming
pattern. Explicit retention applies across export/session restarts.

Hourly rotation with a 256 MiB per-file fallback is a practical default for
long captures; busy channels can still produce multiple files per hour. Output
is streamed to disk, so a larger file does not allocate that size in RAM or
increase the 8 MiB pending queue. This is an application default, not a microSD
or Raspberry Pi requirement. The duration and per-file size remain configurable.
Without explicit retention, storage accounting keeps byte totals rather than an
unbounded in-memory file list; publication/download catalogs remain bounded.

An exclusive directory writer lease prevents concurrent writers; after obtaining
it, a new writer removes only owned reserved-name partials left by a crashed
worker. Closed files are published with an atomic no-replace hard link after
flush/close, with mode 0600. This requires hard-link support; it is not an fsync
or power-loss durability guarantee.

The export writer is a separate spawned process. Source delivery never writes
export files or waits for queue space. The producer admits only complete beacon
blocks and bounded section/interface context (256 KiB) to a byte- and record-
bounded queue. Every original block is delivered to the decoder first. Queue
pressure skips export beacons with visible accounting; disk-full, low-space,
metadata or writer failures disable only export. No disk operation is performed
under the source-admission lock. Stop/session cleanup waits at most two seconds
for the export writer before terminating it and reporting incomplete active
output. Such a partial is never registered for download. A worker also notices
capture-owner death and exits; a stuck filesystem may still prevent immediate
worker exit, in which case its lease prevents unsafe concurrent cleanup.

Status publishes requested/active state, current path, successfully closed
beacon count, active-file count, skipped beacons, queue pressure/queued bytes,
retained bytes/deletions, container selection counts and errors/incomplete
output. Closed counts include segments subsequently removed manually or by opt-in retention; they
are cumulative export progress, not the number of currently downloadable
beacons. After a storage fault, turn export off, resolve storage availability,
and turn it back on. Unsupported/truncated/malformed packet counts remain
separate; they do not alter analyzer decisions or metric definitions.

## One source, unchanged analysis

Direct managed capture uses one `dumpcap -i <interface> -s 0 -n -w -` source,
with the existing 16 MiB capture buffer and namespace routing. RadioSession
still owns tuning/restoration. There is no capture filter. A bounded block
reader forwards the original pcapng stream to one `tshark -r -` decoder and
classifies only the export branch. All data, retry, control and management
frames remain available to the existing decoder/parser/analyzer. The Core
transport feeds this same fan-out boundary when export is configured; Core's
source, ownership validation, optional dependency and stop protocol remain.
The aggregation worker uses spawn/descriptor transfer for the threaded fan-out
and Core paths rather than forking a process containing those threads.

This uses documented [Dumpcap stdout/full-packet capture options](https://www.wireshark.org/docs/man-pages/dumpcap.html)
and [TShark stream decoding](https://www.wireshark.org/docs/man-pages/tshark.html).
The shared reader retains its 4 MiB maximum block. Invalid container structure
is a source failure and is reported; unsupported export options or malformed
packet contents affect only export selection. No missing bytes are invented
when a source provides a truncated packet.

## Use the offline helper

Use an existing **closed, full-packet pcapng** diagnostic capture, such as a file
created by the existing `--raw-pcapng` option. The helper reads that file once;
it does not open an interface, retune a radio or control a session.

```bash
python3 scripts/export_beacons.py source.pcapng utilization_beacons.pcapng
tshark -r utilization_beacons.pcapng -T fields \
  -e frame.time_epoch -e wlan.bssid -e wlan.ssid
```

The command prints JSON counts and the source/output paths. An existing
destination is refused, including the source itself. Output is written to an
owned 0600 temporary file in the destination directory, flushed/closed, then
published with an atomic hard link that cannot replace a concurrent writer's
file. The temporary name is removed on success or failure. The destination
filesystem must support hard links. Malformed input, write failure or failed
publication returns an error without publishing an owned partial capture.
Do not use a source file still being written.

The helper is independent of the web download catalog. Its files are not
registered for web download; use the live controls above for session exports.

## Fidelity and supported input

`beacon_live/pcapng.py` reads bounded blocks and selects complete beacon packet
blocks. It supports pcapng 1.0, both byte orders, appended sections, multiple
interfaces, Enhanced Packet Blocks and obsolete timestamped Packet Blocks.
It copies selected packet blocks byte for byte, including timestamps, packet
options, packet padding, full 802.11 bytes, FCS bytes when captured, and opaque
radiotap data. No packet is rebuilt from `BeaconRecord` or JSONL fields.

Every interface description is copied in its original order, so original
interface IDs and timestamp metadata keep their meaning. Decimal/binary
timestamp resolution and signed offsets remain intact; selection never rounds
timestamps through floating point. Section headers retain their options, with
the section-length field changed to unspecified because filtering changes the
section's byte extent. See the
[pcapng format draft, revision 05](https://www.ietf.org/archive/id/draft-ietf-opsawg-pcapng-05.html)
(work in progress) and [radiotap format](https://radiotap.org/).

Beacon classification uses the original version/type/subtype and radiotap
header length, including extended presence maps. It does not decode vendor
information or infer AP identities. Export preserves a beacon with bad-FCS
metadata when its header is still classifiable; the existing analyzer's
bad-FCS exclusion remains independent and unchanged.

| Input condition | Export behavior |
| --- | --- |
| Complete radiotap/802.11 beacon | Copy its original packet block. |
| Complete raw 802.11 beacon, link type 105 | Copy without synthesizing radiotap. |
| Data, retry, control or other management frame | Omit from export; count as non-beacon. |
| Capture length shorter than original length | Omit; count as truncated. |
| Unsupported link type | Omit packets and count them; retain its interface definition to preserve numbering. |
| Short/invalid radiotap or beacon header | Omit; count as malformed packet. |
| Simple Packet Block | Omit and count as untimestamped; no timestamp is invented. |
| Source interface statistics, secrets, name-resolution or unknown metadata blocks | Omit; count discarded metadata blocks. |
| Invalid block/options/section lengths, undefined interfaces or unsupported container version | Fail export explicitly. |
| Custom options marked non-copyable | Fail explicitly in this first implementation. |

Original per-packet loss/options metadata still describes the source capture.
The JSON export counters describe this selection; source interface statistics
are not presented as beacon-export counts. Non-copyable custom options need a
future metadata rewrite policy rather than silently copying invalid references.

The default maximum block is 4 MiB and the interface-context limit is 256 per
section. Short stream reads and short writes are supported. The reader keeps
one current block, and the selector retains bounded interface context rather
than a packet history or all source bytes. Limits are configurable through the
Python API; an oversized block is refused before its declared body is read.

## Evidence and integration boundary

Synthetic tests cover exact packet-block equality, both byte orders, multiple
sections/interfaces, nanosecond and binary clocks, offsets, opaque vendor
radiotap/FCS data, malformed input, bounds, unsupported/truncated packets and
short/stalled writes. Offline file tests cover existing destinations, malformed
source, disk-full failure, publication races and temporary-file cleanup.

Independent TShark checks decode the generated exports and compare beacon
timestamps, interface IDs, radiotap length, RSSI and SSID against TShark's own
beacon selection from the source, including nanosecond timestamps and offsets.
The actual direct capture facade is also exercised with a synthetic packet
source and real TShark: all 27 mixed frames reach the existing analyzer while
three beacons are exported. Export on/off produces identical snapshots: four
retry-eligible frames, three retries, one observed client and 12 advertised
stations in the completed sample. Core's actual spawn transport, fragmented
WebSocket stream, shared fan-out, spawned analyzer and stop confirmation are
checked against a loopback mock. Exported Core packet blocks equal the source
beacon blocks exactly. These are hardware-free contracts, not Pi performance
or physical radio acceptance.

`write_beacons` performs blocking I/O and runs in the isolated live export worker,
with bounded queues and visible failure/skip accounting. The offline helper calls
it directly. It never writes files on the ordered decoder/analyzer delivery path.

Live integration now connects the shared source, bounded worker, rotation,
retention, status and downloads. Device acceptance remains: install on Pi 4/5,
start a fresh session, toggle export and CSV/JSONL independently while several
consumers attach, wait for a time rollover, stop export and download at least
two closed segments (use a shorter explicit rotation for a quick test). Decode every segment with TShark and check beacon-only
contents, original times/radiotap and continuity of retry/client history during
busy traffic. Exercise supported direct/Core lifecycle, stop during startup,
service restart and radio restoration. Use a disposable/test filesystem for
low-space/failure checks; automated slow/full-disk, queue-overflow and bounded
shutdown tests cover the corresponding isolation contracts locally.

WiFi-Beacon-analysis end-to-end validation is deferred to later testing and is
not a 2.1.0 release gate. Export/download and opening files in Wireshark have been
tested; analytical compatibility/results in that external project are not yet
validated. For that later check, use a pinned offline project revision, supported
AP-name information, AP-to-classroom mapping, classroom metadata, schedule rows
covering the capture, and explicit date/timezone context as described in the
[roadmap](managed-runtime-roadmap.md#wifi-beacon-analysis-end-to-end-check).
