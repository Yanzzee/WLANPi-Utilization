from beacon_live.analyzer import Analyzer
from beacon_live.channel import ChannelDefinition
from beacon_live.channel import ChannelWidth
from beacon_live.models import FrameRecord


TARGET = "aa:aa:aa:aa:aa:aa"
OTHER = "bb:bb:bb:bb:bb:bb"


def test_retry_scope_excludes_pre_discovery_unknown_and_secondary_bssid() -> None:
    analyzer = Analyzer(
        target_primary_frequency_mhz=5180,
        channel_definition_max_age_seconds=10.0,
    )
    frames = (
        _data(1000.01, TARGET, retry=True),
        _beacon(1000.10, TARGET, 5180),
        _data(1000.20, TARGET, retry=True),
        _beacon(1000.30, OTHER, 5200),
        _data(1000.40, OTHER, retry=True),
        _data(1000.50, "cc:cc:cc:cc:cc:cc", retry=True),
        _data(1000.60, TARGET, retry=False),
    )
    for frame in frames:
        analyzer.ingest(frame, publish_snapshot=False)

    stats = analyzer.flush(include_history=False)[0]

    assert stats.received_frame_count == 7
    assert stats.retry_eligible_frame_count == 2
    assert stats.retry_frame_count == 1
    assert stats.retry_percent == 50.0
    assert stats.top_retry_bssid == TARGET


def test_retry_scope_expires_stale_bssid_channel_definition() -> None:
    analyzer = Analyzer(
        target_primary_frequency_mhz=5180,
        channel_definition_max_age_seconds=5.0,
    )
    analyzer.ingest(_beacon(1000.0, TARGET, 5180), publish_snapshot=False)
    analyzer.ingest(_data(1004.9, TARGET, retry=True), publish_snapshot=False)
    analyzer.ingest(_data(1005.1, TARGET, retry=True), publish_snapshot=False)

    stats = {row.second: row for row in analyzer.flush(include_history=False)}

    assert stats[1004].retry_eligible_frame_count == 1
    assert stats[1004].retry_frame_count == 1
    assert stats[1005].retry_eligible_frame_count == 0
    assert stats[1005].retry_frame_count == 0


def test_retry_scope_associates_target_through_ta_ra_sa_and_da() -> None:
    analyzer = Analyzer(target_primary_frequency_mhz=5180)
    analyzer.ingest(_beacon(1000.0, TARGET, 5180), publish_snapshot=False)
    for index, field in enumerate(("ta", "ra", "sa", "da"), start=1):
        values = {field: TARGET}
        analyzer.ingest(
            FrameRecord(
                timestamp=1000.0 + index / 10,
                frame_type=2,
                frame_subtype=0,
                retry_flag=True,
                transmitter_address=values.get("ta"),
                receiver_address=values.get("ra", "00:11:22:33:44:55"),
                source_address=values.get("sa"),
                destination_address=values.get("da", "00:11:22:33:44:55"),
            ),
            publish_snapshot=False,
        )

    stats = analyzer.flush(include_history=False)[0]
    assert stats.retry_eligible_frame_count == 4
    assert stats.retry_frame_count == 4


def _beacon(timestamp: float, bssid: str, primary_frequency: int) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        frame_type=0,
        frame_subtype=8,
        retry_flag=False,
        receiver_address="ff:ff:ff:ff:ff:ff",
        channel_definition=ChannelDefinition(
            primary_frequency,
            ChannelWidth.MHZ80,
            5210,
            primary_channel=36 if primary_frequency == 5180 else 40,
            phy="test",
        ),
    )


def _data(timestamp: float, bssid: str, *, retry: bool) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        frame_type=2,
        frame_subtype=0,
        retry_flag=retry,
        receiver_address="00:11:22:33:44:55",
        destination_address="00:11:22:33:44:55",
    )
