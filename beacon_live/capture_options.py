"""Fixed channel geometry for the web setup form; hardware remains authoritative.

5 GHz operating classes 116–130 and 6 GHz 132–135:
https://kernel.googlesource.com/pub/scm/network/wireless/iwd/+/refs/tags/3.4/src/band.c
Overlapping 320 MHz geometry and nonadjacent 80+80 segments:
https://github.com/torvalds/linux/blob/v6.12/net/wireless/chan.c
"""
from beacon_live.channel import PRIMARY_CHANNELS

WIDTHS = {
    "2.4": ("20", "40"),
    "5": ("20", "40", "80", "160", "80+80"),
    "6": ("20", "40", "80", "160", "80+80", "320"),
}
CENTERS = {
    "2.4": {"40": tuple(range(3, 12))},
    "5": {
        "40": (38, 46, 54, 62, 102, 110, 118, 126, 134, 142, 151, 159, 167, 175),
        "80": (42, 58, 106, 122, 138, 155, 171),
        "160": (50, 114, 163),
    },
    "6": {
        "40": tuple(range(3, 228, 8)), "80": tuple(range(7, 216, 16)),
        "160": tuple(range(15, 208, 32)), "320": tuple(range(31, 192, 32)),
    },
}


def frequency(band: str, channel: int) -> int:
    return 2484 if band == "2.4" and channel == 14 else {"2.4": 2407, "5": 5000, "6": 5950}[band] + 5 * channel


def center_choices(band: str, primary: int, width: str) -> tuple[int, ...]:
    if band not in WIDTHS or primary not in PRIMARY_CHANNELS[band] or width not in WIDTHS[band]:
        return ()
    if width == "20":
        return (frequency(band, primary),)
    size = 80 if width == "80+80" else int(width)
    source = CENTERS[band]["80" if width == "80+80" else width]
    return tuple(frequency(band, channel) for channel in source
                 if frequency(band, primary) in range(frequency(band, channel) - size // 2 + 10,
                                                     frequency(band, channel) + size // 2, 20))


def validate_selection(band, primary, width, center1, center2):
    if width is None:
        return  # The existing runtime supplies the band default.
    if width not in WIDTHS[band]:
        raise ValueError("Capture width is not supported in this band")
    if width == "20":
        if center1 not in (None, frequency(band, primary)) or center2 is not None:
            raise ValueError("20 MHz uses the primary channel center")
        return
    if center1 not in center_choices(band, primary, width):
        raise ValueError("Select a valid center frequency containing the primary channel")
    if width == "80+80":
        if center2 not in tuple(frequency(band, c) for c in CENTERS[band]["80"]) or abs(center1 - center2) <= 80:
            raise ValueError("80+80 requires two nonadjacent 80 MHz segments")
    elif center2 is not None:
        raise ValueError("A second center is valid only for 80+80 MHz")


def web_channel_plan():
    from beacon_live.live import default_channel_definition
    result = {}
    for band, widths in WIDTHS.items():
        defaults = {}
        for channel in PRIMARY_CHANNELS[band]:
            definition = default_channel_definition(primary_frequency_mhz=frequency(band, channel), band=band)
            defaults[str(channel)] = {"width": definition.width.value, "frequency": definition.primary_frequency_mhz}
        default_width = defaults[str(PRIMARY_CHANNELS[band][0])]["width"]
        choices = {}
        for width in ("", *widths):
            entries = []
            for primary in PRIMARY_CHANNELS[band]:
                centers = center_choices(band, primary, width or default_width)
                if not centers:
                    continue
                label = f"{primary}" + (" (PSC)" if band == "6" and (primary - 5) % 16 == 0 else "")
                if band == "2.4" and width == "40":
                    for center in centers:
                        center_channel = (center - 2407) // 5
                        secondary = 2 * center_channel - primary
                        entries.append({"value": f"{primary}:{center}", "primary": primary, "center": center,
                                        "label": f"{primary} + {secondary} · center {center} MHz (Ch {center_channel})"})
                else:
                    entries.append({"value": str(primary), "primary": primary, "label": label})
            choices[width] = entries
        result[band] = {
            "widths": widths, "defaults": defaults, "default_width": default_width, "channels": choices,
            "centers": {width: [{"frequency": frequency(band, c), "channel": c} for c in channels]
                        for width, channels in CENTERS[band].items()},
        }
    return result
