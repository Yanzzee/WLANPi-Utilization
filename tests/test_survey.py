import pytest

from beacon_live.models import SurveySample
from beacon_live.survey import compute_local_cu_percent, parse_survey_dump


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
        )
    ]


def test_compute_local_cu_percent_uses_busy_over_active_delta() -> None:
    previous = SurveySample(1.0, 1000, 200, 100, 20, -95)
    current = SurveySample(2.0, 1500, 350, 200, 35, -94)

    assert compute_local_cu_percent(previous, current) == pytest.approx(30.0)


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
