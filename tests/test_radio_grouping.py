from typing import Optional

from beacon_live.models import BeaconRecord
from beacon_live.models import BssidState
from beacon_live.radio_grouping import estimate_radio_groups
from beacon_live.radio_grouping import likely_same_radio
from beacon_live.radio_grouping import related_bssid_macs
from beacon_live.radio_grouping import select_strongest_radio


def test_adjacent_mac_and_similar_rssi_group_together() -> None:
    first = _state("00:11:22:33:44:50", -40)
    second = _state("00:11:22:33:44:51", -42)

    groups = estimate_radio_groups((first, second))

    assert related_bssid_macs(first.bssid, second.bssid)
    assert likely_same_radio(first, second)
    assert len(groups) == 1
    assert groups[0].bssids == (first.bssid, second.bssid)


def test_mac_addresses_differing_by_one_hex_digit_group_together() -> None:
    first = _state("00:11:22:33:44:50", -40)
    second = _state("00:11:22:33:49:50", -42)

    assert related_bssid_macs(first.bssid, second.bssid)
    assert len(estimate_radio_groups((first, second))) == 1


def test_oui_mismatch_always_prevents_grouping() -> None:
    first = _state("00:11:22:33:44:50", -40, ap_name="Room-101")
    second = _state("00:11:23:33:44:51", -40, ap_name="Room-101")

    assert not likely_same_radio(first, second)
    assert len(estimate_radio_groups((first, second))) == 2


def test_matching_ap_name_groups_nonadjacent_macs_within_ten_db() -> None:
    first = _state("00:11:22:10:20:30", -40, ap_name="Room-101")
    second = _state("00:11:22:a0:b0:c0", -50, ap_name="room-101")

    assert likely_same_radio(first, second)
    assert len(estimate_radio_groups((first, second))) == 1


def test_matching_ap_name_does_not_group_when_rssi_differs_over_ten_db() -> None:
    first = _state("00:11:22:10:20:30", -40, ap_name="Room-101")
    second = _state("00:11:22:a0:b0:c0", -51, ap_name="Room-101")

    assert not likely_same_radio(first, second)
    assert len(estimate_radio_groups((first, second))) == 2


def test_different_ap_names_prevent_otherwise_similar_grouping() -> None:
    first = _state("00:11:22:33:44:50", -40, ap_name="Room-101")
    second = _state("00:11:22:33:44:51", -41, ap_name="Room-102")

    assert not likely_same_radio(first, second)


def test_strongest_radio_selection_uses_rssi_and_keeps_current_on_tie() -> None:
    weak = _state("00:11:22:33:44:50", -60)
    tied_first = _state("00:11:23:33:44:50", -35)
    tied_current = _state("00:11:24:33:44:50", -35)
    groups = estimate_radio_groups((weak, tied_first, tied_current))

    selected = select_strongest_radio(
        groups,
        current_bssids=(tied_current.bssid,),
    )

    assert selected is not None
    assert selected.bssids == (tied_current.bssid,)


def _state(
    bssid: str,
    rssi_dbm: int,
    *,
    ap_name: Optional[str] = None,
    vendor: Optional[str] = None,
) -> BssidState:
    beacon = BeaconRecord(
        timestamp=1000.0,
        ssid="Test",
        bssid=bssid,
        qbss_cu_raw=64,
        qbss_cu_percent=64 / 255 * 100,
        qbss_station_count=1,
        qbss_admission_capacity=10_000,
        rssi_dbm=rssi_dbm,
        ap_name=ap_name,
        vendor=vendor,
    )
    return BssidState(
        bssid=bssid,
        ssid=beacon.ssid,
        last_seen_ts=beacon.timestamp,
        latest_beacon_ts=beacon.timestamp,
        latest_beacon_record=beacon,
        window_beacon_count=1,
        latest_station_count=beacon.qbss_station_count,
        latest_qbss_cu_raw=beacon.qbss_cu_raw,
        latest_qbss_cu_percent=beacon.qbss_cu_percent,
        latest_admission_capacity=beacon.qbss_admission_capacity,
        latest_rssi_dbm=rssi_dbm,
        peak_rssi_dbm=rssi_dbm,
        latest_ap_name=ap_name,
        latest_vendor=vendor,
    )
