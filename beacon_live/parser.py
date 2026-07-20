"""Parsing helpers for legacy beacon and lightweight all-frame TShark output."""

from typing import Iterable, Iterator, Optional, Union

from beacon_live.models import BeaconRecord
from beacon_live.models import FrameRecord
from beacon_live.models import QBSS_ADMISSION_CAPACITY_MAX

TSHARK_FIELD_NAMES = (
    "frame.time_epoch",
    "wlan.ssid",
    "wlan.bssid",
    "wlan.qbss.cu",
    "wlan.qbss.scount",
    "wlan.qbss.adc",
    "radiotap.dbm_antsignal",
)

EXPECTED_TSHARK_FIELD_COUNT = len(TSHARK_FIELD_NAMES)
LEGACY_TSHARK_FIELD_COUNT = EXPECTED_TSHARK_FIELD_COUNT - 1

TSHARK_FRAME_FIELD_NAMES = (
    "frame.time_epoch",
    "wlan.fc.type",
    "wlan.fc.subtype",
    "wlan.fc.retry",
    "wlan.bssid",
    "wlan.ta",
    "wlan.ra",
    "wlan.sa",
    "wlan.da",
    "wlan.ssid",
    "wlan.qbss.cu",
    "wlan.qbss.scount",
    "wlan.qbss.adc",
    "radiotap.dbm_antsignal",
    "wlan.fixed.beacon",
    "wlan.cisco.ccx1.name",
    "wlan.vs.aruba.ap_name",
    "wlan.vs.extreme.ap_name",
    "wlan.vs.aerohive.hostname",
    "wlan.bssid_resolved",
)

EXPECTED_TSHARK_FRAME_FIELD_COUNT = len(TSHARK_FRAME_FIELD_NAMES)
# Older exports included the unused ``frame.len`` field. Keep accepting those
# rows so replay and retry-debug inputs remain backward compatible while the
# live command emits one less field for every captured frame.
PRE_PERFORMANCE_TSHARK_FRAME_FIELD_COUNT = EXPECTED_TSHARK_FRAME_FIELD_COUNT + 1
PHASE_THREE_TSHARK_FRAME_FIELD_COUNT = 16
LEGACY_TSHARK_FRAME_FIELD_COUNT = 12


def parse_tshark_row(row: str) -> Optional[BeaconRecord]:
    """Parse one tab-separated TShark beacon row.

    Returns None when the row is malformed. Empty SSID and QBSS fields are
    represented as None.
    """
    line = row.rstrip("\r\n")
    if not line:
        return None

    fields = line.split("\t")
    if len(fields) not in (LEGACY_TSHARK_FIELD_COUNT, EXPECTED_TSHARK_FIELD_COUNT):
        return None

    timestamp_text, ssid_text, bssid_text, cu_text, scount_text, adc_text = fields[:6]
    rssi_text = fields[6] if len(fields) == EXPECTED_TSHARK_FIELD_COUNT else ""

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

    qbss_admission_capacity = _parse_optional_int(
        adc_text,
        minimum=0,
        maximum=QBSS_ADMISSION_CAPACITY_MAX,
    )
    if qbss_admission_capacity is _MALFORMED:
        return None

    rssi_dbm = _parse_optional_int(rssi_text, minimum=-200, maximum=100)
    if rssi_dbm is _MALFORMED:
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
        rssi_dbm=rssi_dbm,
    )


def parse_tshark_rows(rows: Iterable[str]) -> Iterator[BeaconRecord]:
    """Yield valid beacon records from an iterable of TSV lines."""
    for row in rows:
        record = parse_tshark_row(row)
        if record is not None:
            yield record


def parse_tshark_frame_row(row: str) -> Optional[FrameRecord]:
    """Parse one lightweight all-frame TShark row for live analysis."""
    line = row.rstrip("\r\n")
    if not line:
        return None

    fields = line.split("\t")
    if len(fields) not in (
        LEGACY_TSHARK_FRAME_FIELD_COUNT,
        PHASE_THREE_TSHARK_FRAME_FIELD_COUNT,
        EXPECTED_TSHARK_FRAME_FIELD_COUNT,
        PRE_PERFORMANCE_TSHARK_FRAME_FIELD_COUNT,
    ):
        return None

    if len(fields) == EXPECTED_TSHARK_FRAME_FIELD_COUNT:
        (
            timestamp_text,
            frame_type_text,
            frame_subtype_text,
            retry_text,
            bssid_text,
            transmitter_text,
            receiver_text,
            source_text,
            destination_text,
            ssid_text,
            cu_text,
            scount_text,
            adc_text,
            rssi_text,
            beacon_interval_text,
            cisco_ap_name_text,
            aruba_ap_name_text,
            extreme_ap_name_text,
            aerohive_ap_name_text,
            resolved_bssid_text,
        ) = fields
        frame_length_text = ""
    elif len(fields) == PRE_PERFORMANCE_TSHARK_FRAME_FIELD_COUNT:
        (
            timestamp_text,
            frame_type_text,
            frame_subtype_text,
            retry_text,
            bssid_text,
            transmitter_text,
            receiver_text,
            source_text,
            destination_text,
            ssid_text,
            cu_text,
            scount_text,
            adc_text,
            rssi_text,
            frame_length_text,
            beacon_interval_text,
            cisco_ap_name_text,
            aruba_ap_name_text,
            extreme_ap_name_text,
            aerohive_ap_name_text,
            resolved_bssid_text,
        ) = fields
    elif len(fields) == PHASE_THREE_TSHARK_FRAME_FIELD_COUNT:
        (
            timestamp_text,
            frame_type_text,
            frame_subtype_text,
            retry_text,
            bssid_text,
            transmitter_text,
            receiver_text,
            source_text,
            destination_text,
            ssid_text,
            cu_text,
            scount_text,
            adc_text,
            rssi_text,
            frame_length_text,
            beacon_interval_text,
        ) = fields
        cisco_ap_name_text = aruba_ap_name_text = ""
        extreme_ap_name_text = aerohive_ap_name_text = ""
        resolved_bssid_text = ""
    else:
        (
            timestamp_text,
            frame_type_text,
            frame_subtype_text,
            retry_text,
            bssid_text,
            ssid_text,
            cu_text,
            scount_text,
            adc_text,
            rssi_text,
            frame_length_text,
            beacon_interval_text,
        ) = fields
        transmitter_text = receiver_text = source_text = destination_text = ""
        cisco_ap_name_text = aruba_ap_name_text = ""
        extreme_ap_name_text = aerohive_ap_name_text = ""
        resolved_bssid_text = ""

    try:
        timestamp = float(timestamp_text)
    except ValueError:
        return None

    frame_type = _parse_optional_int(frame_type_text, minimum=0, maximum=3)
    frame_subtype = _parse_optional_int(
        frame_subtype_text,
        minimum=0,
        maximum=15,
    )
    retry_flag = _parse_optional_bool(retry_text)
    qbss_cu_raw = _parse_optional_int(cu_text, minimum=0, maximum=255)
    qbss_station_count = _parse_optional_int(scount_text, minimum=0)
    qbss_admission_capacity = _parse_optional_int(
        adc_text,
        minimum=0,
        maximum=QBSS_ADMISSION_CAPACITY_MAX,
    )
    rssi_dbm = _parse_optional_int(rssi_text, minimum=-200, maximum=100)
    frame_length = _parse_optional_int(frame_length_text, minimum=0)
    beacon_interval_tu = _parse_optional_int(beacon_interval_text, minimum=1)

    parsed_fields = (
        frame_type,
        frame_subtype,
        retry_flag,
        qbss_cu_raw,
        qbss_station_count,
        qbss_admission_capacity,
        rssi_dbm,
        frame_length,
        beacon_interval_tu,
    )
    if any(value is _MALFORMED for value in parsed_fields):
        return None

    qbss_cu_percent = (
        qbss_cu_raw / 255 * 100 if qbss_cu_raw is not None else None
    )
    bssid = _parse_optional_mac(bssid_text)
    ap_name, ie_vendor = _vendor_ap_identity(
        cisco_ap_name_text,
        aruba_ap_name_text,
        extreme_ap_name_text,
        aerohive_ap_name_text,
    )
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        ssid=ssid_text if ssid_text != "" else None,
        rssi_dbm=rssi_dbm,
        frame_type=frame_type,
        frame_subtype=frame_subtype,
        retry_flag=retry_flag,
        frame_length=frame_length,
        beacon_interval_tu=beacon_interval_tu,
        qbss_cu_raw=qbss_cu_raw,
        qbss_cu_percent=qbss_cu_percent,
        qbss_station_count=qbss_station_count,
        qbss_admission_capacity=qbss_admission_capacity,
        transmitter_address=_parse_optional_mac(transmitter_text),
        receiver_address=_parse_optional_mac(receiver_text),
        source_address=_parse_optional_mac(source_text),
        destination_address=_parse_optional_mac(destination_text),
        ap_name=ap_name,
        vendor=(
            ie_vendor
            or _vendor_from_resolved_bssid(resolved_bssid_text, bssid)
        ),
    )


def parse_tshark_frame_rows(rows: Iterable[str]) -> Iterator[FrameRecord]:
    for row in rows:
        record = parse_tshark_frame_row(row)
        if record is not None:
            yield record


class _Malformed:
    pass


_MALFORMED = _Malformed()


def _parse_optional_bool(value: str) -> Union[bool, None, _Malformed]:
    text = value.strip().lower()
    if text == "":
        return None
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return _MALFORMED


def _parse_optional_mac(value: str) -> Optional[str]:
    text = value.strip()
    return text.lower() if text else None


def _vendor_ap_identity(
    cisco_name: str,
    aruba_name: str,
    extreme_name: str,
    aerohive_name: str,
) -> tuple[Optional[str], Optional[str]]:
    candidates = (
        (cisco_name, "Cisco"),
        (aruba_name, "Aruba"),
        (extreme_name, "Extreme Networks"),
        (aerohive_name, "Aerohive"),
    )
    for value, vendor in candidates:
        name = value.strip()
        if name:
            return name, vendor
    return None, None


def _vendor_from_resolved_bssid(
    value: str,
    bssid: Optional[str],
) -> Optional[str]:
    resolved = value.strip()
    if not resolved:
        return None
    if bssid is not None and resolved.casefold() == bssid.casefold():
        return None
    if "_" not in resolved:
        return None
    vendor = resolved.rsplit("_", 1)[0].strip()
    return vendor or None


def _parse_optional_int(
    value: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Union[int, None, _Malformed]:
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
