"""Parsing helpers for saved TShark beacon output."""

from collections.abc import Iterable, Iterator

from beacon_live.models import BeaconRecord

TSHARK_FIELD_NAMES = (
    "frame.time_epoch",
    "wlan.ssid",
    "wlan.bssid",
    "wlan.qbss.cu",
    "wlan.qbss.scount",
    "wlan.qbss.adc",
)

EXPECTED_TSHARK_FIELD_COUNT = len(TSHARK_FIELD_NAMES)


def parse_tshark_row(row: str) -> BeaconRecord | None:
    """Parse one tab-separated TShark beacon row.

    Returns None when the row is malformed. Empty SSID and QBSS fields are
    represented as None.
    """
    line = row.rstrip("\r\n")
    if not line:
        return None

    fields = line.split("\t")
    if len(fields) != EXPECTED_TSHARK_FIELD_COUNT:
        return None

    timestamp_text, ssid_text, bssid_text, cu_text, scount_text, adc_text = fields

    try:
        timestamp = float(timestamp_text)
    except ValueError:
        return None

    bssid = bssid_text.strip()
    if not bssid:
        return None

    qbss_cu_raw = _parse_optional_int(cu_text, minimum=0, maximum=255)
    if qbss_cu_raw is _MALFORMED:
        return None

    qbss_station_count = _parse_optional_int(scount_text, minimum=0)
    if qbss_station_count is _MALFORMED:
        return None

    qbss_admission_capacity = _parse_optional_int(adc_text, minimum=0)
    if qbss_admission_capacity is _MALFORMED:
        return None

    qbss_cu_percent = (
        qbss_cu_raw / 255 * 100 if qbss_cu_raw is not None else None
    )

    return BeaconRecord(
        timestamp=timestamp,
        ssid=ssid_text if ssid_text != "" else None,
        bssid=bssid,
        qbss_cu_raw=qbss_cu_raw,
        qbss_cu_percent=qbss_cu_percent,
        qbss_station_count=qbss_station_count,
        qbss_admission_capacity=qbss_admission_capacity,
    )


def parse_tshark_rows(rows: Iterable[str]) -> Iterator[BeaconRecord]:
    """Yield valid beacon records from an iterable of TSV lines."""
    for row in rows:
        record = parse_tshark_row(row)
        if record is not None:
            yield record


class _Malformed:
    pass


_MALFORMED = _Malformed()


def _parse_optional_int(
    value: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None | _Malformed:
    text = value.strip()
    if text == "":
        return None

    try:
        parsed = int(text)
    except ValueError:
        return _MALFORMED

    if minimum is not None and parsed < minimum:
        return _MALFORMED
    if maximum is not None and parsed > maximum:
        return _MALFORMED
    return parsed
