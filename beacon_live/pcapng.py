"""Bounded, lossless packet-block selection for beacon pcapng exports.

This is an export branch, never an analyzer input or another capture source.
Packet blocks and interface options are copied rather than reconstructed from
decoded fields. Blocking stream I/O belongs in an offline tool/export worker.
Format reference: draft-ietf-opsawg-pcapng-05 (work in progress).
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from fractions import Fraction
import struct
from typing import BinaryIO, Iterator

SHB = 0x0A0D0D0A
IDB = 1
PACKET = 2  # Obsolete Packet Block; still has an original timestamp.
SIMPLE_PACKET = 3
ENHANCED_PACKET = 6
IEEE802_11 = 105
RADIOTAP = 127
DEFAULT_MAX_BLOCK_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_INTERFACES = 256


class PcapngError(ValueError):
    """Malformed/unsupported container; never reinterpret it as valid output."""


@dataclass(frozen=True)
class Block:
    kind: int
    endian: str
    raw: bytes
    offset: int

    @property
    def body(self) -> memoryview:
        return memoryview(self.raw)[8:-4]


@dataclass(frozen=True)
class Interface:
    linktype: int
    snaplen: int
    timestamp_resolution: int = 6
    timestamp_offset_seconds: int = 0

    def timestamp(self, ticks: int) -> Fraction:
        """Exact seconds; selection itself never converts or rounds timestamps."""
        resolution = self.timestamp_resolution
        denominator = 2 ** (resolution & 0x7F) if resolution & 0x80 else 10 ** resolution
        return Fraction(ticks, denominator) + self.timestamp_offset_seconds


@dataclass(frozen=True)
class ExportStats:
    sections: int = 0
    interfaces: int = 0
    packet_blocks: int = 0
    beacons: int = 0
    non_beacons: int = 0
    truncated_packets: int = 0
    unsupported_link_packets: int = 0
    malformed_packets: int = 0
    untimestamped_packets: int = 0
    discarded_metadata_blocks: int = 0
    selected_bytes: int = 0


def _read_exact(stream: BinaryIO, size: int, *, eof_ok: bool = False) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if chunk is None:
            raise BlockingIOError("pcapng reader requires a blocking binary stream")
        if not chunk:
            if eof_ok and not data:
                return b""
            raise PcapngError("Truncated pcapng block")
        if len(chunk) > size - len(data):
            raise PcapngError("Input stream exceeded the requested read size")
        data.extend(chunk)
    return bytes(data)


def iter_blocks(stream: BinaryIO, *, max_block_bytes: int = DEFAULT_MAX_BLOCK_BYTES) -> Iterator[Block]:
    """Read at most one bounded block at a time, including short pipe reads.

    Validate both length words, section byte order/version and declared section
    extents. No input buffer or list grows with the duration of a capture.
    """
    if max_block_bytes < 28:
        raise ValueError("Block limit must be at least 28 bytes")
    endian = None
    offset = 0
    remaining = None
    while True:
        header = _read_exact(stream, 8, eof_ok=True)
        if not header:
            if endian is None:
                raise PcapngError("Missing pcapng section header")
            if remaining not in (None, 0):
                raise PcapngError("Truncated declared pcapng section")
            return
        section = header[:4] == b"\x0a\x0d\x0d\x0a"
        prefix = header
        if section:
            if remaining not in (None, 0):
                raise PcapngError("Section header before declared section end")
            magic = _read_exact(stream, 4)
            endian = {b"\x4d\x3c\x2b\x1a": "<", b"\x1a\x2b\x3c\x4d": ">"}.get(magic)
            if endian is None:
                raise PcapngError("Invalid pcapng byte-order magic")
            prefix += magic
        elif endian is None:
            raise PcapngError("Input must begin with a pcapng section header")
        kind, size = struct.unpack(endian + "II", header)
        if size < (28 if section else 12) or size % 4 or size > max_block_bytes:
            raise PcapngError(f"Invalid/oversized pcapng block length at byte {offset}: {size}")
        if not section and remaining is not None and size > remaining:
            raise PcapngError("Block exceeds declared pcapng section length")
        raw = prefix + _read_exact(stream, size - len(prefix))
        if struct.unpack_from(endian + "I", raw, size - 4)[0] != size:
            raise PcapngError(f"Mismatched pcapng block lengths at byte {offset}")
        if section:
            major, minor, length = struct.unpack_from(endian + "HHq", raw, 12)
            if (major, minor) != (1, 0):
                raise PcapngError(f"Unsupported pcapng version {major}.{minor}")
            if length < -1 or (length >= 0 and length % 4):
                raise PcapngError("Invalid declared pcapng section length")
            remaining = None if length == -1 else length
        elif remaining is not None:
            remaining -= size
        yield Block(kind, endian, raw, offset)
        offset += size


def _options(data: memoryview, endian: str):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise PcapngError("Truncated pcapng option header")
        code, length = struct.unpack_from(endian + "HH", data, offset)
        offset += 4
        padded = (length + 3) & ~3
        if padded > len(data) - offset:
            raise PcapngError("Pcapng option exceeds block bounds")
        if code == 0:
            if length or offset != len(data):
                raise PcapngError("Invalid pcapng end-of-options marker")
            return
        if code in {19372, 19373}:
            raise PcapngError("Non-copyable custom pcapng options are not supported for export")
        if code in {2988, 2989} and length < 4:
            raise PcapngError("Custom pcapng option has no enterprise number")
        yield code, data[offset:offset + length]
        offset += padded
    # Readers also accept an option list ending exactly at the block boundary.


def _validate_options(data: memoryview, endian: str) -> None:
    for _ in _options(data, endian):
        pass


def _beacon_kind(packet: memoryview, linktype: int) -> str:
    if linktype == RADIOTAP:
        if len(packet) < 8 or packet[0] != 0:
            return "malformed_packets"
        length = struct.unpack_from("<H", packet, 2)[0]
        if not 8 <= length <= len(packet):
            return "malformed_packets"
        present_offset = 4
        while struct.unpack_from("<I", packet, present_offset)[0] & 0x80000000:
            present_offset += 4
            if present_offset + 4 > length:
                return "malformed_packets"
        packet = packet[length:]
    elif linktype != IEEE802_11:
        return "unsupported_link_packets"
    if len(packet) < 2 or packet[0] & 3:
        return "malformed_packets"
    if packet[0] != 0x80:  # Version 0, management type, beacon subtype.
        return "non_beacons"
    # Management header plus TSF, beacon interval and capability fields.
    return "beacons" if len(packet) >= 36 else "malformed_packets"


class BeaconFilter:
    """Select complete beacon packet blocks without changing their bytes.

    Keep every IDB in its original order, including unused/unsupported links,
    so packet interface IDs and timestamp metadata retain their exact meaning.
    Unknown metadata, secrets and source interface statistics are omitted.
    """
    def __init__(self, *, max_interfaces: int = DEFAULT_MAX_INTERFACES):
        if max_interfaces < 1:
            raise ValueError("Interface limit must be positive")
        self.max_interfaces = max_interfaces
        self.interfaces: list[Interface] = []
        self.endian = None
        self._counts = {field.name: 0 for field in fields(ExportStats)}

    @property
    def stats(self) -> ExportStats:
        return ExportStats(**self._counts)

    def select(self, block: Block) -> bytes | None:
        body, endian = block.body, block.endian
        if block.kind == SHB:
            _validate_options(body[16:], endian)
            self.endian = endian
            self.interfaces.clear()
            self._counts["sections"] += 1
            # Filtering changes section extents. Keep all SHB options but mark
            # section length unspecified rather than retain a false byte count.
            output = block.raw[:16] + struct.pack(endian + "q", -1) + block.raw[24:]
        else:
            if self.endian != endian:
                raise PcapngError("Block has no matching section context")
            if block.kind == IDB:
                if len(body) < 8:
                    raise PcapngError("Short interface description block")
                if len(self.interfaces) >= self.max_interfaces:
                    raise PcapngError("Pcapng interface limit exceeded")
                linktype, reserved, snaplen = struct.unpack_from(endian + "HHI", body)
                if reserved:
                    raise PcapngError("Nonzero interface reserved field")
                resolution, time_offset, seen = 6, 0, set()
                for code, value in _options(body[8:], endian):
                    if code in {9, 14}:
                        if code in seen or len(value) != (1 if code == 9 else 8):
                            raise PcapngError("Invalid/duplicate interface timestamp option")
                        seen.add(code)
                        if code == 9:
                            resolution = value[0]
                        else:
                            time_offset = struct.unpack(endian + "q", value)[0]
                self.interfaces.append(Interface(linktype, snaplen, resolution, time_offset))
                self._counts["interfaces"] += 1
                output = block.raw
            elif block.kind in {ENHANCED_PACKET, PACKET, SIMPLE_PACKET}:
                self._counts["packet_blocks"] += 1
                if not self._packet_is_beacon(block):
                    return None
                output = block.raw
            else:
                self._counts["discarded_metadata_blocks"] += 1
                return None
        self._counts["selected_bytes"] += len(output)
        return output

    def _packet_is_beacon(self, block: Block) -> bool:
        body, endian = block.body, block.endian
        if block.kind == SIMPLE_PACKET:
            if not self.interfaces or len(body) < 4:
                raise PcapngError("Simple packet has no interface or packet length")
            original = struct.unpack_from(endian + "I", body)[0]
            snaplen = self.interfaces[0].snaplen
            captured = min(original, snaplen) if snaplen else original
            if len(body) != 4 + ((captured + 3) & ~3):
                raise PcapngError("Invalid simple packet length")
            self._counts["untimestamped_packets"] += 1
            return False
        if len(body) < 20:
            raise PcapngError("Short timestamped packet block")
        if block.kind == ENHANCED_PACKET:
            interface, _, _, captured, original = struct.unpack_from(endian + "IIIII", body)
        else:
            interface, _, _, _, captured, original = struct.unpack_from(endian + "HHIIII", body)
        if interface >= len(self.interfaces):
            raise PcapngError("Packet references an undefined interface")
        description = self.interfaces[interface]
        end = 20 + ((captured + 3) & ~3)
        if end > len(body) or captured > original or (description.snaplen and captured > description.snaplen):
            raise PcapngError("Invalid captured/original packet length")
        _validate_options(body[end:], endian)
        if captured < original:
            self._counts["truncated_packets"] += 1
            return False
        kind = _beacon_kind(body[20:20 + captured], description.linktype)
        self._counts[kind] += 1
        return kind == "beacons"


def write_beacons(source: BinaryIO, destination: BinaryIO, *,
                  max_block_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
                  max_interfaces: int = DEFAULT_MAX_INTERFACES) -> ExportStats:
    """Filter an existing pcapng stream; caller owns flush/close/publication.

    Writes may block. A live owner must isolate this in a bounded export worker;
    this function must never run on the live decoder/analyzer delivery path.
    """
    selector = BeaconFilter(max_interfaces=max_interfaces)
    for block in iter_blocks(source, max_block_bytes=max_block_bytes):
        output = selector.select(block)
        if output is None:
            continue
        pending = memoryview(output)
        while pending:
            size = destination.write(pending)
            if size is None or not 0 < size <= len(pending):
                raise OSError("Beacon export writer made no valid progress")
            pending = pending[size:]
    return selector.stats
