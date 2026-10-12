import pytest

from beacon_live.models import SurveySample
from beacon_live.survey import (
    compute_local_cu_percent,
    compute_local_cu_percent_from_samples,
    compute_local_cu_result_from_samples,
    parse_survey_dump,
)


def test_parse_survey_dump_extracts_present_fields() -> None:
    samples = parse_survey_dump(
        """
Survey data from wlan0
        in use
        channel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
        time: 1000 ms
        time busy: 250 ms
        time receive: 125 ms
        time transmit: 50 ms
        noise: -94 dBm
""",
        timestamp=123.0,
    )

    assert samples == [
        SurveySample(
            timestamp=123.0,
            active_ms=1000,
            busy_ms=250,
            receive_ms=125,
            transmit_ms=50,
            noise_dbm=-94,
            frequency_mhz=2437,
            in_use=True,
        )
    ]


def test_parse_survey_dump_extracts_real_iw_channel_time_fields() -> None:
    samples = parse_survey_dump(
        """
Survey data from wlan0
        frequency:                      5955 MHz
        channel active time:            2031631 ms
        channel busy time:              4039 ms
        channel receive time:           12 ms
        channel transmit time:          3 ms
""",
        timestamp=456.0,
    )

    assert samples == [
        SurveySample(
            timestamp=456.0,
            active_ms=2031631,
            busy_ms=4039,
            receive_ms=12,
            transmit_ms=3,
            noise_dbm=None,
            frequency_mhz=5955,
        )
    ]


def test_compute_local_cu_percent_uses_busy_over_active_delta() -> None:
    previous = SurveySample(1.0, 1000, 200, 100, 20, -95)
    current = SurveySample(2.0, 1500, 350, 200, 35, -94)

    assert compute_local_cu_percent(previous, current) == pytest.approx(30.0)


def test_compute_local_cu_percent_from_samples_uses_largest_active_delta() -> None:
    previous_samples = [
        SurveySample(1.0, 1000, 100, 0, 0, None),
        SurveySample(1.0, 5000, 1000, 0, 0, None),
    ]
    current_samples = [
        SurveySample(2.0, 2000, 300, 0, 0, None),
        SurveySample(2.0, 5100, 1010, 0, 0, None),
    ]

    assert compute_local_cu_percent_from_samples(
        previous_samples,
        current_samples,
    ) == pytest.approx(20.0)


def test_compute_local_cu_percent_from_samples_prefers_in_use_entry() -> None:
    previous_samples = [
        SurveySample(1.0, 1000, 100, 0, 0, None, 5180, True),
        SurveySample(1.0, 5000, 1000, 0, 0, None, 5200, False),
    ]
    current_samples = [
        SurveySample(2.0, 2000, 300, 0, 0, None, 5180, True),
        SurveySample(2.0, 7000, 1100, 0, 0, None, 5200, False),
    ]

    assert compute_local_cu_percent_from_samples(
        previous_samples,
        current_samples,
    ) == pytest.approx(20.0)


def test_compute_local_cu_percent_from_samples_uses_target_frequency() -> None:
    previous_samples = [
        SurveySample(1.0, 1000, 100, 0, 0, None, 5180),
        SurveySample(1.0, 5000, 1000, 0, 0, None, 5955),
    ]
    current_samples = [
        SurveySample(2.0, 1000, 100, 0, 0, None, 5180),
        SurveySample(2.0, 6000, 1200, 0, 0, None, 5955),
    ]

    result = compute_local_cu_result_from_samples(
        previous_samples,
        current_samples,
        target_frequency_mhz=5180,
    )

    assert result.local_cu_percent is None
    assert result.frequency_mhz == 5180
    assert result.reason == "target frequency 5180 MHz counters unavailable"


def test_compute_local_cu_result_includes_selected_survey_deltas() -> None:
    previous_samples = [
        SurveySample(1.0, 1000, 100, 0, 0, None, 5180),
    ]
    current_samples = [
        SurveySample(2.0, 2000, 125, 0, 0, None, 5180),
    ]

    result = compute_local_cu_result_from_samples(
        previous_samples,
        current_samples,
        target_frequency_mhz=5180,
    )

    assert result.local_cu_percent == pytest.approx(2.5)
    assert result.frequency_mhz == 5180
    assert result.active_delta_ms == 1000
    assert result.busy_delta_ms == 25
    assert result.reason == "ok"


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (
            SurveySample(1.0, None, 200, 100, 20, -95),
            SurveySample(2.0, 1500, 350, 200, 35, -94),
        ),
        (
            SurveySample(1.0, 1000, 200, 100, 20, -95),
            SurveySample(2.0, 1000, 350, 200, 35, -94),
        ),
        (
            SurveySample(1.0, 1000, 200, 100, 20, -95),
            SurveySample(2.0, 900, 350, 200, 35, -94),
        ),
        (
            SurveySample(1.0, 1000, 200, 100, 20, -95),
            SurveySample(2.0, 1500, 150, 200, 35, -94),
        ),
    ],
)
def test_compute_local_cu_percent_returns_none_for_missing_or_reset_counters(
    previous: SurveySample,
    current: SurveySample,
) -> None:
    assert compute_local_cu_percent(previous, current) is None


def test_inline_in_use_marker_selects_actual_tuned_channel():
    before = parse_survey_dump('''Survey data from wlanpi0
 frequency: 5180.0 MHz [in use]
 channel active time: 100 ms
 channel busy time: 10 ms
Survey data from wlanpi0
 frequency: 5200 MHz
 channel active time: 1000 ms
 channel busy time: 100 ms
''')
    after = parse_survey_dump('''Survey data from wlanpi0
 frequency: 5180.0 MHz [in use]
 channel active time: 200 ms
 channel busy time: 40 ms
Survey data from wlanpi0
 frequency: 5200 MHz
 channel active time: 2000 ms
 channel busy time: 200 ms
''')
    result = compute_local_cu_result_from_samples(before, after)
    assert result.frequency_mhz == 5180
    assert result.local_cu_percent == 30.0


@pytest.mark.parametrize('target', [None, 5180, 5200])
def test_channel_change_does_not_pair_unrelated_counters(target):
    before = SurveySample(1, 100, 10, None, None, None, 5180)
    after = SurveySample(2, 200, 50, None, None, None, 5200)
    assert compute_local_cu_percent(before, after) is None
    assert compute_local_cu_result_from_samples([before], [after], target_frequency_mhz=target).local_cu_percent is None


def test_impossible_busy_delta_is_unavailable_not_over_100_percent():
    before = SurveySample(1, 100, 10, None, None, None, 5955)
    after = SurveySample(2, 200, 150, None, None, None, 5955)
    assert compute_local_cu_percent(before, after) is None
    result = compute_local_cu_result_from_samples([before], [after], target_frequency_mhz=5955)
    assert result.local_cu_percent is None and result.reason != 'ok'


@pytest.mark.parametrize('before_frequency,after_frequency', [(5180, None), (None, 5180)])
def test_one_missing_channel_identity_cannot_establish_matching_counters(before_frequency, after_frequency):
    before = SurveySample(1, 100, 10, None, None, None, before_frequency)
    after = SurveySample(2, 200, 50, None, None, None, after_frequency)
    assert compute_local_cu_percent(before, after) is None
    assert compute_local_cu_result_from_samples([before], [after]).local_cu_percent is None
