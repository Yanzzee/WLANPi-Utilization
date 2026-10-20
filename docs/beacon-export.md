# Beacon pcapng export foundation

Milestone 5 begins with a hardware-free pcapng reader/filter/writer and an
offline helper. This step establishes packet fidelity before changing direct
capture delivery. The live capture/analyzer pipeline, direct default, optional
Core backend and metric definitions are unchanged. Live export controls,
rotation, disk budgets, worker isolation and web pcapng downloads remain the
next implementation step.

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

The helper is independent of the web download catalog. Its files are not yet
registered for web download, and it is not a live export toggle.

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
A synthetic source fan-out retains all 27 original mixed frames for the existing
decoder/analyzer while exporting three beacons. Export on/off produces identical
snapshots: four retry-eligible frames, three retries, one observed client and
12 advertised stations in the completed sample. This proves the hardware-free
contract; it is not a deployed live fan-out or a Pi performance measurement.

`write_beacons` performs blocking I/O. It must run in an isolated export worker
when live integration is added, with bounded queues and visible failure/skip
accounting. It must never run on the ordered decoder/analyzer delivery path.

Next, connect one full-packet source to both the existing decoder and this
export branch, preserving all required frame types for analysis. Add bounded
worker delivery, independently readable rotated files, retention/free-space
limits, health publications and registered downloads. Test slow/full disks and
queue pressure while analysis/history continue. Repeat physical radio and
direct/Core lifecycle checks after capture-source integration.

The WiFi-Beacon-analysis end-to-end gate remains milestone 6. TShark readability
does not establish that gate. Use a pinned offline project revision, supported
AP-name information, AP-to-classroom mapping, classroom metadata, schedule rows
covering the capture, and explicit date/timezone context as described in the
[roadmap](managed-runtime-roadmap.md#wifi-beacon-analysis-end-to-end-check).
