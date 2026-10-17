"""Corrupt download headers must not manufacture stations or beacon BSSIDs."""
from dataclasses import replace
import shutil
from pathlib import Path
import struct
import subprocess
import zlib

import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.live import available_tshark_fields, build_tshark_stream_command, select_tshark_frame_fields
from beacon_live.models import FrameRecord
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES, parse_tshark_capture_record, parse_tshark_live_record
from beacon_live.retry_debug import analyze_retry_frames, build_retry_debug_tshark_command

AP = "02:00:00:00:00:10"
CLIENT = "02:00:00:00:00:20"


@pytest.mark.parametrize("status", [None, 1, 2, 3])
@pytest.mark.parametrize("flag", [None, False])
def test_missing_unverified_or_absent_fcs_keeps_valid_frames(status, flag):
    frame = FrameRecord(timestamp=1000, frame_type=2, frame_subtype=8,
        retry_flag=True, receiver_address=CLIENT, fcs_status=status, radiotap_bad_fcs=flag)
    assert not frame.has_bad_fcs and frame.retry_eligible
    assert replace(frame, frame_type=0, frame_subtype=8, bssid=AP).beacon_record() is not None


def row(values, fields=TSHARK_FRAME_FIELD_NAMES):
    return "\t".join(str(values.get(name, "")) for name in fields)


@pytest.mark.parametrize("parse", [parse_tshark_capture_record, parse_tshark_live_record])
@pytest.mark.parametrize("timestamp", ["nan", "inf", "-inf"])
def test_nonfinite_timestamps_cannot_poison_capture_watermark(parse, timestamp):
    assert parse(row({"frame.time_epoch": timestamp, "wlan.fc.type": 2,
                      "wlan.fc.subtype": 0, "wlan.ra": CLIENT})) == ()


@pytest.mark.parametrize("parse", [parse_tshark_capture_record, parse_tshark_live_record])
def test_repeated_addresses_in_one_beacon_do_not_create_frames(parse):
    frames = parse(row({"frame.time_epoch": 1000.2, "wlan.fc.type": 0,
        "wlan.fc.subtype": 8, "wlan.bssid": AP + "|02:00:00:00:00:99",
        "wlan.ta": AP, "wlan.ra": "ff:ff:ff:ff:ff:ff", "wlan.qbss.scount": 3}))
    assert len(frames) == 1 and frames[0].bssid == AP
    assert frames[0].qbss_station_count == 3


def test_flat_occurrences_never_clone_ap_address_or_retry_into_other_headers():
    frames = parse_tshark_capture_record(row({"frame.time_epoch": 1000.2,
        "wlan.fc.type": "2|2", "wlan.fc.subtype": "8|8", "wlan.fc.retry": "1",
        "wlan.bssid": AP, "wlan.ta": AP, "wlan.ra": CLIENT + "|02:00:00:00:00:30"}))
    assert len(frames) == 2
    assert all(frame.transmitter_address is None and frame.bssid is None for frame in frames)
    assert all(frame.retry_flag is None for frame in frames)


def test_retry_audit_accounts_for_bad_fcs_separately():
    frame = FrameRecord(timestamp=1000.1, frame_type=2, frame_subtype=0,
        retry_flag=True, receiver_address=CLIENT)
    audit = next(item for item in analyze_retry_frames((frame, replace(frame, fcs_status=0)))
                 if item.scope == "channel")
    assert audit.decoded_frame_count == 2
    assert audit.bad_fcs_excluded_count == 1
    assert audit.retry_eligible_frame_count == 1 and audit.retry_percent == 100


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status,flag", [(0, None), (None, True), (1, True)])
def test_bad_fcs_cannot_poison_metrics_or_override_latest_valid_beacon(streaming, status, flag):
    analyzer = Analyzer(streaming=streaming)
    beacon = FrameRecord(timestamp=1000.1, bssid=AP, ssid="Classroom", frame_type=0,
        frame_subtype=8, qbss_cu_raw=128, qbss_cu_percent=50, qbss_station_count=3,
        rssi_dbm=-40, receiver_address="ff:ff:ff:ff:ff:ff", transmitter_address=AP)
    analyzer.ingest(beacon, publish_snapshot=False)
    data = FrameRecord(timestamp=1000.2, bssid=AP, frame_type=2, frame_subtype=0,
        retry_flag=True, transmitter_address=AP, receiver_address=CLIENT)
    analyzer.ingest(data, publish_snapshot=False)
    for index in range(1100):
        fake = f"02:77:00:00:{index // 256:02x}:{index % 256:02x}"
        analyzer.ingest(replace(data, timestamp=1000.3 + index / 10000,
            receiver_address=fake, fcs_status=status, radiotap_bad_fcs=flag), publish_snapshot=False)
        analyzer.ingest(replace(beacon, timestamp=1000.3 + index / 10000,
            bssid=fake, ssid=None, qbss_cu_percent=None, qbss_station_count=None,
            rssi_dbm=-10, fcs_status=status, radiotap_bad_fcs=flag), publish_snapshot=False)
    analyzer.ingest(replace(beacon, timestamp=1001.2), publish_snapshot=False)
    completed = analyzer.publish_capture_complete(None)
    assert completed[0].unique_client_mac_count == 1
    assert completed[0].unique_bssid_count == 1
    assert completed[0].retry_eligible_frame_count == 1
    assert completed[0].retry_percent == 100
    assert len(analyzer.snapshot.bssids) == 1
    assert analyzer.snapshot.bssids[0].ssid == "Classroom"


def capture_bytes(*, include_crc=False):
    """A beacon, one real download client, and corrupt random TA/RA headers."""
    def mac(text):
        return bytes.fromhex(text.replace(":", ""))

    def beacon(ap):
        return (b"\x80\x00\x00\x00" + b"\xff" * 6 + mac(ap) * 2 + b"\x00\x00"
                + struct.pack("<QHH", 0, 100, 1) + b"\x00\x09Classroom\x0b\x05\x03\x00\x80\x00\x00")

    def data(client):
        return b"\x08\x02\x00\x00" + mac(client) + mac(AP) + mac("02:33:00:00:00:01") + b"\x00\x00" + b"\x00" * 32

    packets = [(beacon(AP), False), (data(CLIENT), False)]
    for index in range(1100):
        fake = f"02:77:00:00:{index // 256:02x}:{index % 256:02x}"
        packets.append((data(fake), True))
        if index < 200:
            packets.append((beacon(fake), True))
    packets.append((beacon(AP), False))
    output = bytearray(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 127))
    for index, (packet, bad) in enumerate(packets):
        if include_crc:
            checksum = zlib.crc32(packet) ^ int(bad)
            packet += struct.pack("<I", checksum)
        flag = 0x10 if include_crc else 0x40 if bad else 0
        radiotap = struct.pack("<BBHIB", 0, 0, 9, 2, flag)
        payload = radiotap + packet
        timestamp = 1001 if index == len(packets) - 1 else 1000
        output.extend(struct.pack("<IIII", timestamp, 200000 + index, len(payload), len(payload)))
        output.extend(payload)
    return bytes(output)


@pytest.mark.skipif(shutil.which("tshark") is None, reason="Optional real TShark packet regression")
@pytest.mark.parametrize("include_crc", [False, True])
def test_real_tshark_bad_download_capture_keeps_one_client_and_one_bssid(include_crc):
    fields = select_tshark_frame_fields(available_tshark_fields())
    for command, parse in (
        (build_tshark_stream_command(field_names=fields), parse_tshark_live_record),
        (build_retry_debug_tshark_command(Path("-"), field_names=fields), parse_tshark_capture_record),
    ):
        result = subprocess.run(command, input=capture_bytes(include_crc=include_crc), capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr.decode()
        analyzer = Analyzer(streaming=True)
        bad = 0
        for line in result.stdout.decode().splitlines():
            for frame in parse(line, field_names=fields, ssid_encoding="hex"):
                bad += frame.has_bad_fcs
                analyzer.ingest(frame, publish_snapshot=False)
        stats = analyzer.publish_capture_complete(None)
        assert bad == 1300
        assert stats[0].unique_client_mac_count == 1
        assert stats[0].unique_bssid_count == 1
        assert stats[0].qbss_station_count_sum == 3
        assert stats[0].retry_eligible_frame_count == 1
        assert len(analyzer.snapshot.bssids) == 1
