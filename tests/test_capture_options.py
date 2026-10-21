import pytest

from beacon_live.capture_options import CENTERS, WIDTHS, center_choices, validate_selection, web_channel_plan
from beacon_live.channel import PRIMARY_CHANNELS


@pytest.mark.parametrize("band,primary,width,expected", [
    ("2.4", 1, "40", (2422,)), ("2.4", 5, "40", (2422, 2442)),
    ("2.4", 13, "40", (2462,)), ("2.4", 14, "40", ()),
    ("5", 36, "40", (5190,)), ("5", 44, "80", (5210,)),
    ("5", 36, "160", (5250,)), ("5", 177, "160", (5815,)),
    ("5", 100, "160", (5570,)), ("5", 36, "320", ()),
    ("6", 5, "80", (5985,)), ("6", 65, "320", (6265, 6425)),
    ("6", 233, "80", ()), ("6", 233, "20", (7115,)),
])
def test_standard_centers_for_primary_and_width(band, primary, width, expected):
    assert center_choices(band, primary, width) == expected


@pytest.mark.parametrize("band,primary,width,c1,c2", [
    ("2.4", 1, "80", 2442, None), ("5", 36, "320", 5330, None),
    ("5", 44, "80", 5230, None), ("5", 36, "80", 5530, None),
    ("6", 65, "320", 6305, None), ("6", 233, "160", 7065, None),
    ("5", 36, "80+80", 5210, 5210), ("5", 36, "80+80", 5210, 5290),
    ("5", 36, "80+80", 5210, 5985), ("5", 36, "40", 5190, 5530),
    ("5", 36, "20", 5200, None),
])
def test_rejects_off_grid_out_of_band_and_overlapping_segments(band, primary, width, c1, c2):
    with pytest.raises(ValueError):
        validate_selection(band, primary, width, c1, c2)


def test_nonadjacent_80_plus_80_and_overlapping_320_placements_are_available():
    validate_selection("5", 36, "80+80", 5210, 5530)
    validate_selection("6", 65, "320", 6265, None)
    validate_selection("6", 65, "320", 6425, None)
    assert CENTERS["6"]["320"] == (31, 63, 95, 127, 159, 191)


def test_web_defaults_match_runtime_including_edge_fallbacks():
    plan = web_channel_plan()
    assert plan["2.4"]["widths"] == ("20", "40")
    assert "320" not in plan["5"]["widths"]
    assert plan["2.4"]["defaults"]["6"]["width"] == "20"
    assert plan["5"]["defaults"]["36"]["width"] == "80"
    assert plan["6"]["defaults"]["5"]["width"] == "80"
    assert plan["6"]["defaults"]["233"]["width"] == "20"


@pytest.mark.parametrize("band,width", [(band, width) for band, widths in WIDTHS.items() for width in ("", *widths)])
def test_width_first_choices_offer_all_and_only_valid_primaries(band, width):
    plan = web_channel_plan()[band]
    choices = plan["channels"][width]
    effective_width = width or plan["default_width"]
    assert {choice["primary"] for choice in choices} == {
        primary for primary in PRIMARY_CHANNELS[band] if center_choices(band, primary, effective_width)
    }
    assert len({choice["value"] for choice in choices}) == len(choices)
    for choice in choices:
        if "center" in choice:
            validate_selection(band, choice["primary"], width, choice["center"], None)
        else:
            assert choice["value"] == str(choice["primary"])
    if width == "":
        assert all(plan["defaults"][str(choice["primary"])]["width"] == effective_width for choice in choices)


def test_24ghz_combined_choices_preserve_primary_and_both_secondary_directions():
    choices = web_channel_plan()["2.4"]["channels"]["40"]
    assert [choice for choice in choices if choice["primary"] == 6] == [
        {"value": "6:2427", "primary": 6, "center": 2427, "label": "6 + 2 · center 2427 MHz (Ch 4)"},
        {"value": "6:2447", "primary": 6, "center": 2447, "label": "6 + 10 · center 2447 MHz (Ch 8)"},
    ]
    assert not any(choice["primary"] == 14 for choice in choices)


def test_band_edge_primaries_require_explicit_20mhz_in_width_first_form():
    plan = web_channel_plan()["6"]
    assert "233" not in {choice["value"] for choice in plan["channels"][""]}
    assert "233" in {choice["value"] for choice in plan["channels"]["20"]}
    assert next(choice for choice in plan["channels"]["20"] if choice["primary"] == 229)["label"] == "229 (PSC)"
