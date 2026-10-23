"""Shared host identities/counts, signal attribution and rolling expiry."""
from dataclasses import FrozenInstanceError, replace

import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.channel import ChannelDefinition
from beacon_live.host_tracking import MAX_HOST_DETAILS
from beacon_live.models import FrameRecord, HostState, MetricsSnapshot
from beacon_live.ipc import from_wire, to_wire
from beacon_live.live import select_tshark_frame_fields
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES, TSHARK_HOST_FIELD_NAMES, parse_tshark_live_record

AP = "02:00:00:00:00:10"
AP2 = "02:00:00:00:00:20"
CLIENT = "00:11:22:33:44:55"
OTHER = "00:11:22:33:44:66"
LOCAL = "02:11:22:33:44:77"
GROUP = "ff:ff:ff:ff:ff:ff"


def beacon(timestamp, bssid=AP, ssid="Test", primary=2412):
    return FrameRecord(timestamp, bssid=bssid, ssid=ssid, frame_type=0, frame_subtype=8,
                       transmitter_address=bssid, receiver_address=GROUP, rssi_dbm=-40,
                       channel_definition=ChannelDefinition(primary))


def data(timestamp, *, client=CLIENT, ap=AP, tx=True, retry=False, rssi=-55, vendor="Example"):
    return FrameRecord(timestamp, bssid=ap, frame_type=2, frame_subtype=0, retry_flag=retry,
        transmitter_address=client if tx else ap, receiver_address=ap if tx else client,
        source_address=client, destination_address=ap, rssi_dbm=rssi,
        transmitter_vendor=vendor if tx else "AP Vendor", receiver_vendor="AP Vendor" if tx else vendor)


def analyze(frames, *, streaming=True, window=120, primary=None):
    analyzer = Analyzer(streaming=streaming, window_seconds=window, target_primary_frequency_mhz=primary)
    for frame in frames:
        analyzer.ingest(frame, publish_snapshot=False)
    analyzer.ingest(FrameRecord(int(max(frame.timestamp for frame in frames)) + 1.2, frame_type=1, frame_subtype=13), publish_snapshot=False)
    analyzer.publish_capture_complete(None, capture_ended=True)
    return analyzer


@pytest.mark.parametrize("streaming", [True, False])
def test_hosts_use_wireless_data_endpoints_with_correct_direction_and_retry_denominator(streaming):
    analyzer = analyze([beacon(1000.1), data(1001.1), data(1001.2, retry=True),
        data(1001.3, tx=False, rssi=-21), data(1001.4, client=OTHER, tx=False)], streaming=streaming)
    hosts = {row.mac: row for row in analyzer.snapshot.hosts}
    first, received_only = hosts[CLIENT], hosts[OTHER]
    assert first.tx_frames == 2 and first.rx_frames == 1 and first.tx_retry_percent == 50
    assert first.last_activity == 1001.3 and first.last_tx_timestamp == 1001.2
    assert first.last_tx_rssi_dbm == -55  # The AP's -21 dBm downlink is not client RSSI.
    assert first.bssid == AP and first.ssid == "Test" and first.vendor == "Example"
    assert not first.locally_administered
    assert received_only.tx_frames == 0 and received_only.rx_frames == 1
    assert received_only.last_tx_rssi_dbm is received_only.last_tx_timestamp is received_only.tx_retry_percent is None
    assert analyzer.snapshot.window_unique_client_mac_count == len(hosts) == 2
    assert analyzer.snapshot.current.retry_percent == 25  # Existing channel denominator includes downlink.
    with pytest.raises(FrozenInstanceError):
        first.tx_frames = 99
    assert from_wire(MetricsSnapshot, to_wire(analyzer.snapshot)) == analyzer.snapshot


@pytest.mark.parametrize("streaming", [True, False])
def test_bad_fcs_probe_control_group_ap_and_distribution_addresses_do_not_create_hosts(streaming):
    frames = [beacon(1000.1), replace(data(1001.1), radiotap_bad_fcs=True),
        replace(data(1001.2), frame_type=0, frame_subtype=4),
        replace(data(1001.3), frame_type=1, frame_subtype=13),
        replace(data(1001.4), transmitter_address=AP, receiver_address=GROUP),
        replace(data(1001.5), source_address=OTHER, destination_address=LOCAL)]
    analyzer = analyze(frames, streaming=streaming)
    assert [row.mac for row in analyzer.snapshot.hosts] == [CLIENT]
    assert analyzer.snapshot.hosts[0].tx_frames == 1
    assert analyzer.snapshot.window_unique_client_mac_count == 1


@pytest.mark.parametrize("streaming", [True, False])
def test_primary_scope_and_unknown_bssid_keep_existing_client_rules(streaming):
    analyzer = analyze([beacon(1000.1), beacon(1000.2, AP2, primary=2437),
        data(1001.1, ap=AP2), data(1001.2, ap="02:00:00:00:00:99", client=OTHER),
        data(1001.3, client=LOCAL)], streaming=streaming, primary=2412)
    assert [row.mac for row in analyzer.snapshot.hosts] == [LOCAL]
    host = analyzer.snapshot.hosts[0]
    assert host.locally_administered and host.vendor is None
    assert analyzer.snapshot.window_unique_client_mac_count == 1


@pytest.mark.parametrize("streaming", [True, False])
def test_latest_context_and_missing_rssi_do_not_reuse_downlink_or_older_signal(streaming):
    analyzer = analyze([beacon(1000.1), beacon(1000.2, AP2, "Other"), data(1001.1, rssi=-50),
        data(1002.1, ap=AP2, rssi=None), data(1002.2, ap=AP2, tx=False, rssi=-10)], streaming=streaming)
    host = analyzer.snapshot.hosts[0]
    assert host.bssid == AP2 and host.ssid == "Other"
    assert host.last_tx_timestamp == 1002.1 and host.last_tx_rssi_dbm is None
    assert host.last_activity == 1002.2 and host.tx_frames == 2 and host.rx_frames == 1


@pytest.mark.parametrize("streaming", [True, False])
def test_host_counter_buckets_expire_with_the_shared_window(streaming):
    analyzer = Analyzer(streaming=streaming, window_seconds=3)
    for frame in [beacon(1000.1), data(1000.2, retry=True), data(1001.2), beacon(1003.3)]:
        analyzer.ingest(frame, publish_snapshot=False)
    analyzer.advance(1004, None)
    host = analyzer.snapshot.hosts[0]
    assert host.tx_frames == 1 and host.tx_retry_percent == 0  # Older retry bucket expired.
    analyzer.ingest(beacon(1006.3), publish_snapshot=False)
    analyzer.advance(1007, None)
    assert not analyzer.snapshot.hosts and analyzer.snapshot.window_unique_client_mac_count == 0


def test_late_frame_cannot_replace_latest_host_context_or_signal():
    analyzer = analyze([beacon(1000.1), beacon(1000.2, AP2, "Other"), data(1002.2, ap=AP2),
                        data(1001.2, rssi=-90)], streaming=False)
    host = analyzer.snapshot.hosts[0]
    assert host.bssid == AP2 and host.ssid == "Other" and host.last_tx_rssi_dbm == -55
    assert host.tx_frames == 2 and host.last_activity == 1002.2


def test_detail_bounds_do_not_cap_or_change_existing_client_count():
    frames = [beacon(1000.1)]
    for index in range(MAX_HOST_DETAILS + 10):
        mac = ":".join(f"{byte:02x}" for byte in (0, 1, 2, index >> 8, index & 255, 3))
        frames.append(data(1001.1 + index / 100000, client=mac))
    analyzer = analyze(frames)
    assert len(analyzer.snapshot.hosts) == MAX_HOST_DETAILS and analyzer.snapshot.host_details_limited
    assert analyzer.snapshot.window_unique_client_mac_count == MAX_HOST_DETAILS + 10


def test_window_detail_limit_keeps_recent_hosts_and_merges_their_previous_counters():
    def address(index):
        return f"00:11:22:33:{index >> 8:02x}:{index & 255:02x}"

    frames = [beacon(1000.1)]
    for index in range(1200):
        frames.append(data(1001 + index // 600 + .1 + index % 600 / 10000, client=address(index)))
    frames.append(data(1002.9, client=address(0), retry=True))
    snapshot = analyze(frames).snapshot
    hosts = {row.mac: row for row in snapshot.hosts}
    assert len(hosts) == MAX_HOST_DETAILS and snapshot.host_details_limited
    assert snapshot.window_unique_client_mac_count == 1200
    assert address(1) not in hosts and address(1199) in hosts
    assert hosts[address(0)].tx_frames == 2 and hosts[address(0)].tx_retry_percent == 50


def test_missing_retry_bit_keeps_tx_count_with_unavailable_retry_percent():
    host = analyze([beacon(1000.1), data(1001.1, retry=None)]).snapshot.hosts[0]
    assert host.tx_frames == 1 and host.tx_retry_percent is None


def test_vendor_fields_are_optional_and_multi_mpdu_values_remain_aligned():
    assert not any(name in select_tshark_frame_fields(frozenset(TSHARK_FRAME_FIELD_NAMES))
                   for name in TSHARK_HOST_FIELD_NAMES)
    fields = select_tshark_frame_fields(frozenset(TSHARK_FRAME_FIELD_NAMES + TSHARK_HOST_FIELD_NAMES))
    assert all(name in fields for name in TSHARK_HOST_FIELD_NAMES)
    values = {"frame.time_epoch": "1001.25", "wlan.fc.type": "2|2", "wlan.fc.subtype": "0|0",
              "wlan.fc.retry": "0|1", "wlan.bssid": f"{AP}|{AP}", "wlan.ta": f"{CLIENT}|{OTHER}",
              "wlan.ra": f"{AP}|{AP}", "radiotap.dbm_antsignal": "-58",
              "wlan.ta.oui_resolved": "First Vendor|Second Vendor", "wlan.ra.oui_resolved": "AP Vendor|AP Vendor"}
    records = parse_tshark_live_record("\t".join(values.get(name, "") for name in fields), field_names=fields)
    assert [row.transmitter_vendor for row in records] == ["First Vendor", "Second Vendor"]
    assert [row.transmitter_address for row in records] == [CLIENT, OTHER]
    assert [row.rssi_dbm for row in records] == [-58, -58]


@pytest.mark.parametrize("multiple", [False, True])
def test_live_parser_retains_signal_and_optional_manufacturer_fields(multiple):
    fields = TSHARK_FRAME_FIELD_NAMES + TSHARK_HOST_FIELD_NAMES
    values = {"frame.time_epoch": "1001.25", "wlan.fc.type": "2", "wlan.fc.subtype": "0",
              "wlan.fc.retry": "1", "wlan.bssid": AP, "wlan.ta": CLIENT, "wlan.ra": AP,
              "radiotap.dbm_antsignal": "-58|-60" if multiple else "-58",
              "wlan.ta.oui_resolved": "Example Manufacturer", "wlan.ra.oui_resolved": "Other Manufacturer"}
    result = parse_tshark_live_record("\t".join(values.get(name, "") for name in fields), field_names=fields)
    assert len(result) == 1
    assert result[0].rssi_dbm == -58
    assert result[0].transmitter_vendor == "Example Manufacturer"
    assert result[0].receiver_vendor == "Other Manufacturer"
