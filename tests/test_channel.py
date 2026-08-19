import pytest
from typing import Optional

from beacon_live.channel import BssidChannelObservation
from beacon_live.channel import ChannelDefinition
from beacon_live.channel import ChannelWidth
from beacon_live.channel import RadioCapabilities
from beacon_live.channel import RetuneHysteresis
from beacon_live.channel import choose_capture_definition
from beacon_live.channel import definition_from_operation_fields
from beacon_live.channel import parse_iw_interface_channel
from beacon_live.channel import parse_iw_phy_capabilities
from beacon_live.live import build_definition_tune_command
from beacon_live.live import explicit_channel_definition


def test_ht_operation_resolves_20_and_both_40mhz_offsets() -> None:
    ht20 = definition_from_operation_fields(
        band="5", ht_primary_channel=36, ht_secondary_offset=0
    )
    plus = definition_from_operation_fields(
        band="5", ht_primary_channel=36, ht_secondary_offset=1
    )
    minus = definition_from_operation_fields(
        band="5", ht_primary_channel=40, ht_secondary_offset=3
    )

    assert ht20 is not None and ht20.width is ChannelWidth.MHZ20
    assert plus is not None and plus.center_frequency1_mhz == 5190
    assert minus is not None and minus.center_frequency1_mhz == 5190
    assert build_definition_tune_command("mon0", plus)[-1] == "HT40+"
    assert build_definition_tune_command("mon0", minus)[-1] == "HT40-"


@pytest.mark.parametrize(
    ("width_code", "center0", "center1", "expected_width", "expected_centers"),
    [
        (1, 42, None, ChannelWidth.MHZ80, (5210, None)),
        (1, 42, 50, ChannelWidth.MHZ160, (5250, None)),
        (3, 42, 155, ChannelWidth.MHZ80P80, (5210, 5775)),
    ],
)
def test_vht_operation_resolves_80_160_and_80p80(
    width_code: int,
    center0: int,
    center1: Optional[int],
    expected_width: ChannelWidth,
    expected_centers: tuple[int, Optional[int]],
) -> None:
    definition = definition_from_operation_fields(
        band="5",
        ht_primary_channel=36,
        ht_secondary_offset=1,
        vht_width=width_code,
        vht_center0=center0,
        vht_center1=center1,
    )

    assert definition is not None
    assert definition.width is expected_width
    assert (
        definition.center_frequency1_mhz,
        definition.center_frequency2_mhz,
    ) == expected_centers


def test_he_6ghz_and_eht_320_operation_fields() -> None:
    he = definition_from_operation_fields(
        band="6",
        he_primary_channel=5,
        he_width=2,
        he_center0=7,
    )
    eht = definition_from_operation_fields(
        band="6",
        he_primary_channel=1,
        eht_width=4,
        eht_center0=31,
    )
    punctured = definition_from_operation_fields(
        band="6",
        he_primary_channel=1,
        eht_width=4,
        eht_center0=31,
        puncturing_bitmap=0x4,
    )

    assert he is not None
    assert he.primary_frequency_mhz == 5975
    assert he.width is ChannelWidth.MHZ80
    assert he.center_frequency1_mhz == 5985
    assert eht is not None and eht.width is ChannelWidth.MHZ320
    assert eht.center_frequency1_mhz == 6105
    assert punctured is not None and punctured.puncturing_bitmap == 0x4
    unsupported, reason = RadioCapabilities(
        frozenset({ChannelWidth.MHZ320}),
        frozenset(range(5955, 6256, 20)),
        supports_monitor=True,
        supports_puncturing=False,
        source_complete=True,
    ).supports(punctured)
    assert not unsupported
    assert "puncturing" in reason


def test_he_6ghz_uses_capture_frequency_when_primary_field_is_missing() -> None:
    definition = definition_from_operation_fields(
        band="6",
        fallback_primary_frequency_mhz=5975,
        he_width=2,
        he_center0=7,
    )

    assert definition is not None
    assert definition.complete
    assert not definition.ambiguous
    assert definition.primary_channel == 5
    assert definition.primary_frequency_mhz == 5975
    assert definition.width is ChannelWidth.MHZ80
    assert definition.center_frequency1_mhz == 5985


def test_zero_puncturing_and_ambiguous_center_segments_are_conservative() -> None:
    unpunctured = definition_from_operation_fields(
        band="6",
        he_primary_channel=1,
        eht_width=4,
        eht_center0=31,
        puncturing_bitmap=0,
    )
    bad_he = definition_from_operation_fields(
        band="6",
        he_primary_channel=1,
        he_width=3,
        he_center0=7,
        he_center1=11,
    )
    bad_vht = definition_from_operation_fields(
        band="5",
        ht_primary_channel=36,
        vht_width=1,
        vht_center0=42,
        vht_center1=46,
    )

    assert unpunctured is not None and unpunctured.puncturing_bitmap is None
    assert bad_he is not None and bad_he.ambiguous and not bad_he.supported
    assert bad_vht is not None and bad_vht.ambiguous and not bad_vht.supported


def test_iw_capability_and_actual_definition_parsing() -> None:
    capabilities = parse_iw_phy_capabilities(
        """
        Supported interface modes:
             * managed
             * monitor
        HT Capabilities: HT20/HT40
        VHT Capabilities: Supported Channel Width: 160 MHz, 80+80 MHz
        EHT Iftypes: managed
        EHT PHY Capabilities: 320 MHz
        Supported extended features:
             * preamble puncturing in AP mode
        Frequencies:
             * 5180 MHz [36] (23.0 dBm)
             * 5200 MHz [40] (23.0 dBm)
             * 5220 MHz [44] (23.0 dBm)
             * 5240 MHz [48] (23.0 dBm)
        """
    )
    actual = parse_iw_interface_channel(
        "channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz, "
        "punctured: 0x4"
    )

    assert capabilities.supports_monitor
    assert ChannelWidth.MHZ320 in capabilities.supported_widths
    assert capabilities.supports_puncturing
    assert actual is not None
    assert actual.width is ChannelWidth.MHZ80
    assert actual.center_frequency1_mhz == 5210
    assert actual.puncturing_bitmap == 0x4


def test_he_rx_puncturing_does_not_imply_nl80211_channel_support() -> None:
    capabilities = parse_iw_phy_capabilities(
        """
        Supported interface modes:
             * monitor
        HE Iftypes: managed
             Punctured Preamble RX: 0x4
        """
    )

    assert not capabilities.supports_puncturing


def test_widest_nested_definition_covers_different_bssid_widths() -> None:
    capabilities = _capabilities(ChannelWidth.MHZ20, ChannelWidth.MHZ80, ChannelWidth.MHZ160)
    observations = (
        _observation("20", _definition(ChannelWidth.MHZ20, 5180), 100.0),
        _observation("80", _definition(ChannelWidth.MHZ80, 5210), 100.1),
        _observation("160", _definition(ChannelWidth.MHZ160, 5250), 100.2),
    )

    decision = choose_capture_definition(
        observations,
        target_primary_frequency_mhz=5180,
        capabilities=capabilities,
        now=101.0,
        max_age_seconds=10.0,
    )

    assert decision.complete
    assert decision.requested.width is ChannelWidth.MHZ160
    assert decision.covered_bssids == ("160", "20", "80")


def test_conflict_unsupported_mode_and_stale_observation_are_explicit() -> None:
    capabilities = _capabilities(ChannelWidth.MHZ20, ChannelWidth.MHZ80)
    observations = (
        _observation("fresh", _definition(ChannelWidth.MHZ80, 5210), 100.0),
        _observation(
            "unsupported",
            ChannelDefinition(
                5180,
                ChannelWidth.MHZ80P80,
                5210,
                5775,
                primary_channel=36,
                phy="vht",
            ),
            100.0,
        ),
        _observation("stale", _definition(ChannelWidth.MHZ20, 5180), 80.0),
    )

    decision = choose_capture_definition(
        observations,
        target_primary_frequency_mhz=5180,
        capabilities=capabilities,
        now=101.0,
        max_age_seconds=10.0,
    )

    assert decision.status == "partial"
    assert decision.requested.width is ChannelWidth.MHZ80
    assert decision.partial_bssids == ("unsupported",)
    assert "80+80" in (decision.warning or "")
    assert "stale" not in decision.covered_bssids


def test_no_fresh_observations_falls_back_to_ht20() -> None:
    decision = choose_capture_definition(
        (_observation("stale", _definition(ChannelWidth.MHZ80, 5210), 1.0),),
        target_primary_frequency_mhz=5180,
        capabilities=_capabilities(ChannelWidth.MHZ20, ChannelWidth.MHZ80),
        now=20.0,
        max_age_seconds=10.0,
    )

    assert decision.status == "fallback"
    assert decision.requested.width is ChannelWidth.MHZ20
    assert "no fresh" in (decision.warning or "")


def test_retune_hysteresis_rejects_transient_definition() -> None:
    current = _definition(ChannelWidth.MHZ20, 5180)
    requested = _definition(ChannelWidth.MHZ80, 5210)
    hysteresis = RetuneHysteresis(stability_seconds=2.0, minimum_observations=3)

    assert not hysteresis.consider(requested, current, now=0.0)
    assert not hysteresis.consider(requested, current, now=1.0)
    assert hysteresis.consider(requested, current, now=2.0)
    assert not hysteresis.consider(current, current, now=3.0)


def test_explicit_width_requires_centers_and_builds_160_tune() -> None:
    with pytest.raises(ValueError, match="center-frequency1"):
        explicit_channel_definition(
            primary_frequency_mhz=5180,
            width=ChannelWidth.MHZ80,
        )
    definition = explicit_channel_definition(
        primary_frequency_mhz=5180,
        width=ChannelWidth.MHZ160,
        center_frequency1_mhz=5250,
    )
    assert build_definition_tune_command("wlan0", definition) == [
        "iw",
        "dev",
        "wlan0",
        "set",
        "freq",
        "5180",
        "160",
        "5250",
    ]


def test_80mhz_tune_uses_numeric_iw_channel_definition_syntax() -> None:
    definition = ChannelDefinition(
        5200,
        ChannelWidth.MHZ80,
        5210,
        primary_channel=40,
    )

    assert build_definition_tune_command("wlan0", definition) == [
        "iw",
        "dev",
        "wlan0",
        "set",
        "freq",
        "5200",
        "80",
        "5210",
    ]


def test_supported_puncturing_is_passed_to_iw() -> None:
    definition = ChannelDefinition(
        5955,
        ChannelWidth.MHZ320,
        6105,
        primary_channel=1,
        phy="eht",
        puncturing_bitmap=4,
    )

    assert build_definition_tune_command("wlan0", definition)[-2:] == [
        "punct",
        "4",
    ]


def _definition(width: ChannelWidth, center1: int) -> ChannelDefinition:
    return ChannelDefinition(
        5180,
        width,
        center1,
        primary_channel=36,
        phy="test",
    )


def _observation(
    bssid: str,
    definition: ChannelDefinition,
    timestamp: float,
) -> BssidChannelObservation:
    return BssidChannelObservation(bssid, definition, timestamp)


def _capabilities(*widths: ChannelWidth) -> RadioCapabilities:
    return RadioCapabilities(
        frozenset(widths),
        frozenset(range(5000, 5900, 20)),
        supports_monitor=True,
        source_complete=True,
    )
