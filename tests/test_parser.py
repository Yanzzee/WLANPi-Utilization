import pytest

from beacon_live.models import FrameRecord
from beacon_live.parser import parse_tshark_row
from beacon_live.parser import parse_tshark_frame_row
from beacon_live.parser import parse_tshark_capture_record
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
from beacon_live.parser import TSHARK_OPTIONAL_FRAME_FIELD_NAMES


def test_parse_valid_qbss_row_converts_cu_to_percent() -> None:
    record = parse_tshark_row(
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff\t128\t12\t42\t-47"
    )

    assert record is not None
    assert record.timestamp == 1700000000.125
    assert record.ssid == "LabNet"
    assert record.bssid == "aa:bb:cc:dd:ee:ff"
    assert record.qbss_cu_raw == 128
    assert record.qbss_cu_percent == pytest.approx(128 / 255 * 100)
    assert record.qbss_station_count == 12
    assert record.qbss_admission_capacity == 42
    assert record.rssi_dbm == -47


def test_parse_empty_ssid_and_qbss_fields_as_none() -> None:
    record = parse_tshark_row("1700000000.125\t\taa:bb:cc:dd:ee:ff\t\t\t")

    assert record is not None
    assert record.ssid is None
    assert record.qbss_cu_raw is None
    assert record.qbss_cu_percent is None
    assert record.qbss_station_count is None
    assert record.qbss_admission_capacity is None
    assert record.rssi_dbm is None


def test_parse_legacy_row_without_rssi_keeps_replay_compatibility() -> None:
    record = parse_tshark_row(
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff\t128\t12\t42"
    )

    assert record is not None
    assert record.rssi_dbm is None


def test_parse_admission_capacity_accepts_31250_as_maximum() -> None:
    record = parse_tshark_row(
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff\t128\t12\t31250"
    )

    assert record is not None
    assert record.qbss_admission_capacity == 31250


def test_parse_all_frame_beacon_extracts_retry_and_beacon_fields() -> None:
    record = parse_tshark_frame_row(
        "1700000000.125\t0\t8\t0\tAA:BB:CC:DD:EE:FF\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tLabNet\t128\t12\t42\t-47\t256\t100"
    )

    assert record is not None
    assert record.is_beacon
    assert record.retry_flag is False
    assert record.frame_type == 0
    assert record.frame_subtype == 8
    assert record.bssid == "aa:bb:cc:dd:ee:ff"
    assert record.transmitter_address == "aa:bb:cc:dd:ee:ff"
    assert record.receiver_address == "ff:ff:ff:ff:ff:ff"
    assert not record.retry_eligible
    assert record.beacon_interval_tu == 100
    assert record.qbss_cu_percent == pytest.approx(128 / 255 * 100)
    assert record.beacon_record() is not None


def test_parse_phase_four_vendor_ie_ap_name_and_vendor() -> None:
    record = parse_tshark_frame_row(
        "1700000000.125\t0\t8\t0\tAA:BB:CC:DD:EE:FF\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tLabNet\t128\t12\t42\t-47\t256\t100\tRoom-101\t\t\t\tCisco_aa:bb:cc"
    )

    assert record is not None
    assert record.ap_name == "Room-101"
    assert record.vendor == "Cisco"
    beacon = record.beacon_record()
    assert beacon is not None
    assert beacon.ap_name == "Room-101"
    assert beacon.vendor == "Cisco"


def test_parse_phase_four_uses_bundled_oui_resolution_as_vendor_fallback() -> None:
    record = parse_tshark_frame_row(
        "1700000000.125\t0\t8\t0\tAA:BB:CC:DD:EE:FF\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tLabNet\t128\t12\t42\t-47\t256\t100\t\t\t\t\tAcmeWire_aa:bb:cc"
    )

    assert record is not None
    assert record.ap_name is None
    assert record.vendor == "AcmeWire"


def test_parse_optimized_live_row_without_unused_frame_length() -> None:
    record = parse_tshark_frame_row(
        "1700000000.125\t0\t8\t0\tAA:BB:CC:DD:EE:FF\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tAA:BB:CC:DD:EE:FF\tff:ff:ff:ff:ff:ff\tLabNet\t128\t12\t42\t-47\t100\tRoom-101\t\t\t\tCisco_aa:bb:cc"
    )

    assert record is not None
    assert record.frame_length is None
    assert record.beacon_interval_tu == 100
    assert record.qbss_cu_raw == 128
    assert record.qbss_station_count == 12
    assert record.qbss_admission_capacity == 42
    assert record.ap_name == "Room-101"
    assert record.vendor == "Cisco"


def test_parse_all_frame_data_extracts_retry_without_beacon_fields() -> None:
    record = parse_tshark_frame_row(
        "1700000000.250\t2\t0\t1\taa:bb:cc:dd:ee:ff\t11:22:33:44:55:66\taa:bb:cc:dd:ee:ff\t11:22:33:44:55:66\t76:88:99:aa:bb:cc\t\t\t\t\t-51\t128\t"
    )

    assert record is not None
    assert not record.is_beacon
    assert record.retry_flag is True
    assert record.retry_eligible
    assert record.bssid == "aa:bb:cc:dd:ee:ff"
    assert record.mac_addresses == (
        "aa:bb:cc:dd:ee:ff",
        "11:22:33:44:55:66",
        "76:88:99:aa:bb:cc",
    )
    assert record.qbss_cu_raw is None
    assert record.beacon_record() is None


def test_parse_operation_and_packet_diagnostic_fields() -> None:
    field_names = TSHARK_FRAME_FIELD_NAMES + TSHARK_OPTIONAL_FRAME_FIELD_NAMES
    values = {name: "" for name in field_names}
    values.update(
        {
            "frame.time_epoch": "1700000000.125",
            "wlan.fc.type": "0",
            "wlan.fc.subtype": "8",
            "wlan.fc.retry": "0",
            "wlan.bssid": "aa:bb:cc:dd:ee:ff",
            "wlan.ra": "ff:ff:ff:ff:ff:ff",
            "wlan.ssid": "Wide",
            "radiotap.dbm_antsignal": "-45",
            "wlan.fixed.beacon": "100",
            "wlan.ht.info.primarychannel": "36",
            "wlan.ht.info.secchanoffset": "1",
            "wlan.vht.op.channelwidth": "1",
            "wlan.vht.op.channelcenter0": "42",
            "frame.cap_len": "256",
            "frame.len": "512",
            "wlan_radio.frequency": "5180",
            "wlan_radio.phy": "8",
            "radiotap.vht.bw": "4",
            "wlan.fcs.status": "1",
            "wlan.seq": "123",
            "wlan.frag": "2",
            "wlan.qos.tid": "5",
            "radiotap.ampdu.reference": "99",
        }
    )

    records = parse_tshark_capture_record(
        "\t".join(values[name] for name in field_names),
        field_names=field_names,
        band="5",
    )

    assert len(records) == 1
    record = records[0]
    assert record.channel_definition is not None
    assert record.channel_definition.width.value == "80"
    assert record.channel_definition.center_frequency1_mhz == 5210
    assert record.captured_length == 256
    assert record.original_length == 512
    assert record.sequence_number == 123
    assert record.fragment_number == 2
    assert record.qos_tid == 5
    assert record.ampdu_reference == 99
    assert "radiotap.vht.bw=4" in (record.phy_bandwidth or "")


def test_multiple_wlan_occurrences_emit_every_mpdu_and_retry_bit() -> None:
    values = {name: "" for name in TSHARK_FRAME_FIELD_NAMES}
    values.update(
        {
            "frame.time_epoch": "1700000000.250",
            "wlan.fc.type": "2|2",
            "wlan.fc.subtype": "0|0",
            "wlan.fc.retry": "0|1",
            "wlan.bssid": (
                "aa:bb:cc:dd:ee:ff|aa:bb:cc:dd:ee:ff"
            ),
            "wlan.ra": "00:11:22:33:44:55|00:11:22:33:44:55",
            "wlan.da": "00:11:22:33:44:55|00:11:22:33:44:55",
            "radiotap.dbm_antsignal": "-45|-47",
        }
    )

    records = parse_tshark_capture_record(
        "\t".join(values[name] for name in TSHARK_FRAME_FIELD_NAMES)
    )

    assert len(records) == 2
    assert [record.retry_flag for record in records] == [False, True]
    assert [record.rssi_dbm for record in records] == [-45, -45]
    assert all(record.retry_eligible for record in records)


@pytest.mark.parametrize(
    ("frame_control", "expected_retry"),
    (("0x8808", True), ("0x8800", False)),
)
def test_frame_control_fallback_recovers_retry_bit(
    frame_control: str,
    expected_retry: bool,
) -> None:
    field_names = TSHARK_FRAME_FIELD_NAMES + ("wlan.fc",)
    values = {name: "" for name in field_names}
    values.update(
        {
            "frame.time_epoch": "1700000000.250",
            "wlan.fc": frame_control,
            "wlan.fc.type": "2",
            "wlan.fc.subtype": "0",
            "wlan.bssid": "aa:bb:cc:dd:ee:ff",
            "wlan.ra": "00:11:22:33:44:55",
            "wlan.da": "00:11:22:33:44:55",
        }
    )

    records = parse_tshark_capture_record(
        "\t".join(values[name] for name in field_names),
        field_names=field_names,
    )

    assert len(records) == 1
    assert records[0].retry_flag is expected_retry
    assert records[0].retry_eligible


def test_6ghz_beacon_uses_radio_frequency_when_he_primary_is_unavailable() -> None:
    field_names = TSHARK_FRAME_FIELD_NAMES + ("wlan_radio.frequency",)
    values = {name: "" for name in field_names}
    values.update(
        {
            "frame.time_epoch": "1700000000.125",
            "wlan.fc.type": "0",
            "wlan.fc.subtype": "8",
            "wlan.fc.retry": "0",
            "wlan.bssid": "aa:bb:cc:dd:ee:ff",
            "wlan.ra": "ff:ff:ff:ff:ff:ff",
            "wlan.ssid": "Six",
            "wlan.fixed.beacon": "100",
            "wlan_radio.frequency": "5975",
        }
    )

    records = parse_tshark_capture_record(
        "\t".join(values[name] for name in field_names),
        field_names=field_names,
        band="6",
    )

    assert len(records) == 1
    definition = records[0].channel_definition
    assert definition is not None
    assert definition.primary_frequency_mhz == 5975
    assert definition.complete is False
    assert definition.ambiguous is True


def test_parse_legacy_all_frame_row_without_address_fields() -> None:
    record = parse_tshark_frame_row(
        "1700000000.250\t2\t0\t1\taa:bb:cc:dd:ee:ff\t\t\t\t\t-51\t128\t"
    )

    assert record is not None
    assert record.retry_eligible
    assert record.transmitter_address is None
    assert record.receiver_address is None


@pytest.mark.parametrize(
    "record",
    [
        FrameRecord(1.0, frame_type=0, frame_subtype=4, retry_flag=True),
        FrameRecord(1.0, frame_type=0, frame_subtype=8, retry_flag=True),
        FrameRecord(1.0, frame_type=0, frame_subtype=14, retry_flag=True),
        FrameRecord(1.0, frame_type=1, frame_subtype=11, retry_flag=True),
        FrameRecord(1.0, frame_type=3, frame_subtype=0, retry_flag=True),
        FrameRecord(
            1.0,
            frame_type=2,
            frame_subtype=0,
            retry_flag=True,
            receiver_address="ff:ff:ff:ff:ff:ff",
        ),
        FrameRecord(1.0, frame_type=2, frame_subtype=0, retry_flag=None),
    ],
)
def test_retry_eligibility_excludes_nonretryable_frames(
    record: FrameRecord,
) -> None:
    assert not record.retry_eligible


def test_retry_eligibility_accepts_unicast_management_and_data_frames() -> None:
    assert FrameRecord(
        1.0,
        frame_type=0,
        frame_subtype=11,
        retry_flag=True,
        receiver_address="00:11:22:33:44:55",
    ).retry_eligible
    assert FrameRecord(
        1.0,
        frame_type=2,
        frame_subtype=0,
        retry_flag=False,
        receiver_address="00:11:22:33:44:55",
    ).retry_eligible


@pytest.mark.parametrize(
    "row",
    [
        "1700000000.250\t2\t0\tmaybe\taa:bb:cc:dd:ee:ff\t\t\t\t\t\t\t\t\t-51\t128\t",
        "1700000000.250\t2\t16\t1\taa:bb:cc:dd:ee:ff\t\t\t\t\t\t\t\t\t-51\t128\t",
        "1700000000.250\t2\t0\t1\taa:bb:cc:dd:ee:ff",
    ],
)
def test_parse_malformed_all_frame_rows_return_none(row: str) -> None:
    assert parse_tshark_frame_row(row) is None


@pytest.mark.parametrize(
    "row",
    [
        "",
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff",
        "not-a-time\tLabNet\taa:bb:cc:dd:ee:ff\t128\t12\t42",
        "1700000000.125\tLabNet\t\t128\t12\t42",
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff\t300\t12\t42",
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff\t128\tbad\t42",
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff\t128\t12\t31251",
        "1700000000.125\tLabNet\taa:bb:cc:dd:ee:ff\t128\t12\t42\tbad",
    ],
)
def test_parse_malformed_rows_return_none(row: str) -> None:
    assert parse_tshark_row(row) is None
