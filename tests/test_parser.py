import pytest

from beacon_live.parser import parse_tshark_row
from beacon_live.parser import parse_tshark_frame_row


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
        "1700000000.125\t0\t8\t0\taa:bb:cc:dd:ee:ff\tLabNet\t128\t12\t42\t-47\t256\t100"
    )

    assert record is not None
    assert record.is_beacon
    assert record.retry_flag is False
    assert record.frame_type == 0
    assert record.frame_subtype == 8
    assert record.beacon_interval_tu == 100
    assert record.qbss_cu_percent == pytest.approx(128 / 255 * 100)
    assert record.beacon_record() is not None


def test_parse_all_frame_data_extracts_retry_without_beacon_fields() -> None:
    record = parse_tshark_frame_row(
        "1700000000.250\t2\t0\t1\taa:bb:cc:dd:ee:ff\t\t\t\t\t-51\t128\t"
    )

    assert record is not None
    assert not record.is_beacon
    assert record.retry_flag is True
    assert record.bssid == "aa:bb:cc:dd:ee:ff"
    assert record.qbss_cu_raw is None
    assert record.beacon_record() is None


@pytest.mark.parametrize(
    "row",
    [
        "1700000000.250\t2\t0\tmaybe\taa:bb:cc:dd:ee:ff\t\t\t\t\t-51\t128\t",
        "1700000000.250\t2\t16\t1\taa:bb:cc:dd:ee:ff\t\t\t\t\t-51\t128\t",
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
