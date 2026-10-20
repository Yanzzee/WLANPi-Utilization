"""Original packet bytes/time metadata survive selection; malformed input is explicit."""
from dataclasses import FrozenInstanceError
from fractions import Fraction
import io
import struct

import pytest

from beacon_live.pcapng import (
    BeaconFilter, PcapngError, iter_blocks, write_beacons,
    SHB, IDB, PACKET, SIMPLE_PACKET, ENHANCED_PACKET,
)

AP = bytes.fromhex("020000000010")
CLIENT = bytes.fromhex("020000000020")
GROUP = b"\xff" * 6


def option(code, value, endian="<"):
    return struct.pack(endian + "HH", code, len(value)) + value + b"\0" * (-len(value) % 4)


def block(kind, body=b"", endian="<"):
    assert len(body) % 4 == 0
    size = len(body) + 12
    return struct.pack(endian + "II", kind, size) + body + struct.pack(endian + "I", size)


def section(endian="<", length=-1, options=b"", version=(1, 0)):
    return block(SHB, struct.pack(endian + "IHHq", 0x1A2B3C4D, *version, length) + options, endian)


def interface(endian="<", linktype=127, options=b"", snaplen=65535):
    return block(IDB, struct.pack(endian + "HHI", linktype, 0, snaplen) + options, endian)


def wlan_frame(fc=0x80, *, beacon=False, ra=GROUP, ta=AP):
    header = struct.pack("<HH", fc, 0) + ra + ta + AP + b"\0\0"
    if beacon or fc & 0xFC == 0x80:
        return header + struct.pack("<QHH", 0x1122334455667788, 100, 0x411) + b"\x00\x04Test\x0b\x05\x0c\x00\x80\x2a\x00"
    return header + b"\xaa\xaa\x03\x00\x00\x00\x08\x00" + b"\0" * 20


def radiotap(frame=None, *, bad_fcs=False):
    # Flags, channel and RSSI. Payload and metadata must be copied unchanged.
    radio = struct.pack("<BBHIBxHHb", 0, 0, 15, 0x2A, 0x40 if bad_fcs else 0, 2412, 0xA0, -47)
    return radio + (wlan_frame() if frame is None else frame)


def packet(payload=None, *, endian="<", iface=0, ticks=1700000000123456789,
           original=None, options=b"", kind=ENHANCED_PACKET):
    payload = radiotap() if payload is None else payload
    original = len(payload) if original is None else original
    high, low = ticks >> 32, ticks & 0xFFFFFFFF
    fields = struct.pack(endian + ("IIIII" if kind == ENHANCED_PACKET else "HHIIII"),
                         *([iface, high, low, len(payload), original] if kind == ENHANCED_PACKET
                           else [iface, 7, high, low, len(payload), original]))
    return block(kind, fields + payload + b"\0" * (-len(payload) % 4) + options, endian)


def filter_bytes(data, **limits):
    destination = io.BytesIO()
    stats = write_beacons(io.BytesIO(data), destination, **limits)
    return destination.getvalue(), stats


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("resolution", [6, 9, 0x8A])
@pytest.mark.parametrize("kind", [ENHANCED_PACKET, PACKET])
def test_packet_timestamp_radiotap_padding_and_options_are_copied_byte_for_byte(endian, resolution, kind):
    metadata = option(9, bytes([resolution]), endian) + option(14, struct.pack(endian + "q", -123), endian)
    metadata += option(2, b"wlanpi0", endian) + option(2989, struct.pack(endian + "I", 12345) + b"opaque", endian)
    idb = interface(endian, options=metadata)
    beacon = packet(endian=endian, options=option(1, b"packet comment", endian), kind=kind)
    other = packet(radiotap(wlan_frame(0x0908, ra=AP, ta=CLIENT)), endian=endian)
    header = section(endian, length=len(idb + beacon + other), options=option(1, b"capture provenance", endian))
    output, stats = filter_bytes(header + idb + beacon + other)
    result = list(iter_blocks(io.BytesIO(output)))
    assert [item.kind for item in result] == [SHB, IDB, kind]
    assert result[1].raw == idb and result[2].raw == beacon
    assert result[0].raw[:16] == header[:16] and result[0].raw[24:] == header[24:]
    assert struct.unpack_from(endian + "q", result[0].raw, 16)[0] == -1
    selector = BeaconFilter()
    for item in result:
        selector.select(item)
    clock = selector.interfaces[0]
    expected = Fraction(1700000000123456789, 2 ** 10 if resolution == 0x8A else 10 ** resolution) - 123
    assert clock.timestamp(1700000000123456789) == expected
    assert stats.beacons == 1 and stats.non_beacons == 1 and stats.packet_blocks == 2
    assert stats.selected_bytes == len(output)
    with pytest.raises(FrozenInstanceError):
        stats.beacons = 99


def test_multiple_sections_byte_orders_and_interface_indices_remain_meaningful():
    first_ids = interface(linktype=1) + interface(options=option(9, b"\x09"))
    second_ids = interface(">", linktype=105, options=option(14, struct.pack(">q", 99), ">"))
    beacon_first = packet(iface=1)
    beacon_second = packet(wlan_frame(), endian=">", ticks=1000001)
    source = section() + first_ids + packet(b"ethernet", iface=0) + beacon_first
    source += section(">") + second_ids + beacon_second
    output, stats = filter_bytes(source)
    items = list(iter_blocks(io.BytesIO(output)))
    assert [item.kind for item in items] == [SHB, IDB, IDB, ENHANCED_PACKET, SHB, IDB, ENHANCED_PACKET]
    assert items[3].raw == beacon_first and items[-1].raw == beacon_second
    assert [item.endian for item in items] == ["<"] * 4 + [">"] * 3
    assert stats.sections == 2 and stats.interfaces == 3
    assert stats.beacons == 2 and stats.unsupported_link_packets == 1


def test_non_beacon_bad_fcs_truncated_unsupported_and_untimestamped_outcomes_are_explicit():
    beacon = packet(radiotap(bad_fcs=True))
    short = radiotap()[:25]
    simple = radiotap()
    spb = block(SIMPLE_PACKET, struct.pack("<I", len(simple)) + simple + b"\0" * (-len(simple) % 4))
    source = section() + interface() + interface(linktype=1) + beacon
    source += packet(radiotap(wlan_frame(0x50)))  # probe response
    source += packet(radiotap(wlan_frame(0x08))) + packet(short, original=len(radiotap()))
    source += packet(b"not an 802.11 link", iface=1) + spb
    source += packet(b"\x00\x00\x08\x00\x00\x00\x00\x80" + wlan_frame())  # unterminated present bitmap
    source += packet(radiotap(wlan_frame(0x81, beacon=True)))  # unsupported 802.11 protocol version
    output, stats = filter_bytes(source)
    assert [item.raw for item in iter_blocks(io.BytesIO(output)) if item.kind == ENHANCED_PACKET] == [beacon]
    assert (stats.beacons, stats.non_beacons, stats.truncated_packets,
            stats.unsupported_link_packets, stats.untimestamped_packets, stats.malformed_packets) == (1, 2, 1, 1, 1, 2)
    assert stats.packet_blocks == 8


@pytest.mark.parametrize("payload", [b"", b"\0" * 7,
    b"\x01\0\x08\0" + b"\0" * 4, b"\0\0\x07\0" + b"\0" * 4,
    b"\0\0\xff\xff" + b"\0" * 4, radiotap()[:30]])
def test_short_or_malformed_radiotap_and_beacon_headers_are_counted_not_exported(payload):
    output, stats = filter_bytes(section() + interface() + packet(payload))
    assert stats.beacons == 0 and stats.malformed_packets == 1
    assert len(list(iter_blocks(io.BytesIO(output)))) == 2


def test_source_interface_statistics_and_unknown_or_secret_blocks_are_not_export_counts():
    source = section() + interface() + block(5, b"\0" * 12) + block(10, b"\0" * 8)
    source += block(0xBAD, b"opaque!!") + packet()
    output, stats = filter_bytes(source)
    assert [item.kind for item in iter_blocks(io.BytesIO(output))] == [SHB, IDB, ENHANCED_PACKET]
    assert stats.discarded_metadata_blocks == 3


def test_extended_vendor_radiotap_and_original_fcs_are_opaque_and_preserved():
    vendor = b"\x00\x11\x22\x01" + struct.pack("<H", 6) + b"opaque"
    # Extended presence map, FCS-present/bad-FCS flags, alignment, vendor data.
    header = struct.pack("<BBHII", 0, 0, 26, 0xC0000002, 0) + b"\x50\0" + vendor
    original = packet(header + wlan_frame() + b"\xde\xad\xbe\xef")
    output, stats = filter_bytes(section() + interface() + original)
    assert stats.beacons == 1
    assert list(iter_blocks(io.BytesIO(output)))[-1].raw == original


def test_interface_bound_applies_per_section_in_a_long_appended_capture():
    source = (section() + interface() + packet()) * 100
    output, stats = filter_bytes(source, max_interfaces=1)
    assert stats.sections == stats.interfaces == stats.beacons == 100
    assert len(list(iter_blocks(io.BytesIO(output)))) == 300


class ShortReader(io.BytesIO):
    def read(self, size=-1):
        assert size >= 0, "unbounded read"
        return super().read(min(size, 3))


class ShortWriter(io.BytesIO):
    def write(self, data):
        return super().write(data[:5])


def test_short_reads_and_writes_do_not_drop_or_duplicate_packet_bytes():
    source = section() + interface() + packet() + packet(radiotap(wlan_frame(0x08)))
    writer = ShortWriter()
    stats = write_beacons(ShortReader(source), writer)
    assert writer.getvalue() == filter_bytes(source)[0]
    assert stats.beacons == 1 and stats.non_beacons == 1


@pytest.mark.parametrize("data", [b"", b"\0" * 8, section()[:-1], section() + b"\0",
    section()[:8] + b"\0" * 4 + section()[12:],
    section() + struct.pack("<II", 99, 13),
    section(version=(2, 0)), section(length=-2), section(length=1),
    section(length=40) + interface(), section(length=0) + interface(),
    section(length=100) + section(),
    section() + struct.pack("<III", IDB, 16, 12),
    section() + interface() + packet(iface=1),
    section() + interface() + packet(original=1),
    section() + interface(snaplen=8) + packet(),
    section() + block(ENHANCED_PACKET, b"\0" * 16),
    section() + block(IDB, b"\0" * 4),
    section() + block(SIMPLE_PACKET, b"\0" * 4),
    section() + interface() + block(SIMPLE_PACKET, struct.pack("<I", 999)),
])
def test_malformed_container_is_an_error_instead_of_a_successful_partial_export(data):
    with pytest.raises(PcapngError):
        filter_bytes(data)


@pytest.mark.parametrize("options", [struct.pack("<HH", 9, 999), option(9, b"\x06\x09"),
    option(14, b"\0"), option(9, b"\x06") * 2, option(0, b"x"),
    option(0, b"") + option(1, b"after end"), option(2989, b"bad"),
    option(19372, b"\0" * 4), option(19373, b"\0" * 4)])
def test_invalid_or_noncopyable_interface_options_are_explicit_errors(options):
    with pytest.raises(PcapngError):
        filter_bytes(section() + interface(options=options) + packet())


@pytest.mark.parametrize("where", ["section", "packet"])
def test_option_validation_applies_to_section_and_packet_options(where):
    bad = struct.pack("<HH", 1, 100)
    source = section(options=bad) + interface() + packet() if where == "section" else section() + interface() + packet(options=bad)
    with pytest.raises(PcapngError, match="option exceeds"):
        filter_bytes(source)


def test_block_and_interface_limits_reject_before_large_reads_or_growth():
    class Limited(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 256
            return super().read(size)
    source = section() + struct.pack("<II", ENHANCED_PACKET, 0xFFFFFFFC)
    with pytest.raises(PcapngError, match="oversized"):
        list(iter_blocks(Limited(source), max_block_bytes=256))
    with pytest.raises(PcapngError, match="interface limit"):
        filter_bytes(section() + interface() * 3, max_interfaces=2)
    with pytest.raises(ValueError):
        list(iter_blocks(io.BytesIO(section()), max_block_bytes=12))
    with pytest.raises(ValueError):
        BeaconFilter(max_interfaces=0)


@pytest.mark.parametrize("result", [0, None, -1])
def test_stalled_or_failed_writer_does_not_report_success(result):
    class Writer:
        def write(self, data):
            return result
    with pytest.raises(OSError, match="no valid progress"):
        write_beacons(io.BytesIO(section() + interface() + packet()), Writer())
