import pytest

from beacon_live.parser import parse_tshark_row


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
