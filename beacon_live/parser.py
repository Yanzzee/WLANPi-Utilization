"""Parsing helpers for legacy and capability-negotiated TShark output."""

from dataclasses import replace
from typing import Iterable, Iterator, Optional, Union

from beacon_live.channel import definition_from_operation_fields
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

# These fields are appended only when ``tshark -G fields`` reports them. This
# lets one source tree work with the older TShark commonly installed on WLAN Pi
# images while using HE/EHT and packet-audit data on newer versions.
TSHARK_LIVE_OPTIONAL_FRAME_FIELD_NAMES = (
    "wlan.ds.current_channel",
    "wlan.ht.info.primarychannel",
    "wlan.ht.info.secchanoffset",
    "wlan.vht.op.channelwidth",
    "wlan.vht.op.channelcenter0",
    "wlan.vht.op.channelcenter1",
    "wlan.ext_tag.he_operation.6ghz.primary_channel",
    "wlan.ext_tag.he_operation.6ghz.control.channel_width",
    "wlan.ext_tag.he_operation.6ghz.chan_center_freq_seg_0",
    "wlan.ext_tag.he_operation.6ghz.chan_center_freq_seg_1",
    "wlan.eht.eht_operation_information.control.channel_width",
    "wlan.eht.eht_operation_information.ccfs0",
    "wlan.eht.eht_operation_information.ccfs1",
    "wlan.eht.eht_operation_information.disabled_subchannel_bitmap",
    "frame.cap_len",
    "frame.len",
)

TSHARK_DIAGNOSTIC_FRAME_FIELD_NAMES = (
    "wlan_radio.frequency",
    "wlan_radio.phy",
    "radiotap.mcs.bw",
    "radiotap.vht.bw",
    "radiotap.he.data_5.data_bw_ru_allocation",
    "radiotap.he_mu.bw_from_sig_a",
    "radiotap.u_sig.common.bw",
    "radiotap.u_sig.value.mu_ppdu.punctured_channel_information",
    "radiotap.flags.badfcs",
    "wlan.fcs.status",
    "wlan.seq",
    "wlan.frag",
    "wlan.qos.tid",
    "radiotap.ampdu.reference",
)

TSHARK_OPTIONAL_FRAME_FIELD_NAMES = (
    TSHARK_LIVE_OPTIONAL_FRAME_FIELD_NAMES
    + TSHARK_DIAGNOSTIC_FRAME_FIELD_NAMES
)

TSHARK_MULTI_VALUE_SEPARATOR = "|"

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


def parse_tshark_frame_row(
    row: str,
    *,
    field_names: Optional[tuple[str, ...]] = None,
    band: Optional[str] = None,
) -> Optional[FrameRecord]:
    """Parse one lightweight all-frame TShark row for live analysis."""
    line = row.rstrip("\r\n")
    if not line:
        return None

    fields = line.split("\t")
    if (
        field_names is not None
        and tuple(field_names) != TSHARK_FRAME_FIELD_NAMES
        and len(fields) == len(field_names)
    ):
        return _parse_named_tshark_frame_fields(fields, field_names, band=band)
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
        yield from parse_tshark_capture_record(row)


def parse_tshark_capture_record(
    row: str,
    *,
    field_names: tuple[str, ...] = TSHARK_FRAME_FIELD_NAMES,
    band: Optional[str] = None,
) -> tuple[FrameRecord, ...]:
    """Expand every decoded WLAN MPDU occurrence in one capture record.

    Monitor drivers normally deliver each A-MPDU member as its own capture
    record. If a dissector does expose several WLAN headers in one record,
    ``occurrence=a`` produces aligned values and this function emits one
    normalized frame for each instead of silently discarding later Retry bits.
    """
    line = row.rstrip("\r\n")
    if not line:
        return ()
    fields = line.split("\t")
    if len(fields) != len(field_names):
        legacy = parse_tshark_frame_row(row)
        return (legacy,) if legacy is not None else ()

    occurrence_fields = {
        "wlan.fc.type",
        "wlan.fc.subtype",
        "wlan.fc.retry",
        "wlan.bssid",
        "wlan.ta",
        "wlan.ra",
        "wlan.sa",
        "wlan.da",
        "wlan.seq",
        "wlan.frag",
        "wlan.qos.tid",
    }
    split_fields = [value.split(TSHARK_MULTI_VALUE_SEPARATOR) for value in fields]
    occurrence_count = max(
        (
            len(values)
            for name, values in zip(field_names, split_fields)
            if name in occurrence_fields
        ),
        default=1,
    )
    records: list[FrameRecord] = []
    for index in range(occurrence_count):
        expanded = []
        for name, values in zip(field_names, split_fields):
            if name not in occurrence_fields:
                # Radiotap can expose one signal value per antenna. Those are
                # properties of the outer capture record, not extra MPDUs.
                # Preserve occurrence=f behavior for scalar fields so a
                # multi-antenna RSSI never turns into an empty value.
                expanded.append(values[0])
            elif len(values) == occurrence_count:
                expanded.append(values[index])
            elif len(values) == 1:
                expanded.append(values[0])
            else:
                expanded.append("")
        record = parse_tshark_frame_row(
            "\t".join(expanded),
            field_names=field_names,
            band=band,
        )
        if record is not None:
            records.append(record)
    return tuple(records)


def _parse_named_tshark_frame_fields(
    fields: list[str],
    field_names: tuple[str, ...],
    *,
    band: Optional[str],
) -> Optional[FrameRecord]:
    values = dict(zip(field_names, fields))
    core_row = "\t".join(values.get(name, "") for name in TSHARK_FRAME_FIELD_NAMES)
    record = parse_tshark_frame_row(core_row)
    if record is None or record.frame_type is None:
        return None

    integers: dict[str, Optional[int]] = {}
    for name in TSHARK_OPTIONAL_FRAME_FIELD_NAMES:
        parsed = _parse_optional_int(values.get(name, ""), minimum=0)
        if parsed is _MALFORMED:
            return None
        integers[name] = parsed

    radio_frequency = integers.get("wlan_radio.frequency")
    resolved_band = band or _band_from_frequency(radio_frequency)
    definition = None
    if record.is_beacon and resolved_band is not None:
        definition = definition_from_operation_fields(
            band=resolved_band,
            ds_primary_channel=integers.get("wlan.ds.current_channel"),
            ht_primary_channel=integers.get("wlan.ht.info.primarychannel"),
            ht_secondary_offset=integers.get("wlan.ht.info.secchanoffset"),
            vht_width=integers.get("wlan.vht.op.channelwidth"),
            vht_center0=integers.get("wlan.vht.op.channelcenter0"),
            vht_center1=integers.get("wlan.vht.op.channelcenter1"),
            he_primary_channel=integers.get(
                "wlan.ext_tag.he_operation.6ghz.primary_channel"
            ),
            he_width=integers.get(
                "wlan.ext_tag.he_operation.6ghz.control.channel_width"
            ),
            he_center0=integers.get(
                "wlan.ext_tag.he_operation.6ghz.chan_center_freq_seg_0"
            ),
            he_center1=integers.get(
                "wlan.ext_tag.he_operation.6ghz.chan_center_freq_seg_1"
            ),
            eht_width=integers.get(
                "wlan.eht.eht_operation_information.control.channel_width"
            ),
            eht_center0=integers.get(
                "wlan.eht.eht_operation_information.ccfs0"
            ),
            eht_center1=integers.get(
                "wlan.eht.eht_operation_information.ccfs1"
            ),
            puncturing_bitmap=integers.get(
                "wlan.eht.eht_operation_information.disabled_subchannel_bitmap"
            ),
        )

    phy_bandwidth_parts = tuple(
        f"{name}={values[name]}"
        for name in (
            "wlan_radio.phy",
            "radiotap.mcs.bw",
            "radiotap.vht.bw",
            "radiotap.he.data_5.data_bw_ru_allocation",
            "radiotap.he_mu.bw_from_sig_a",
            "radiotap.u_sig.common.bw",
            "radiotap.u_sig.value.mu_ppdu.punctured_channel_information",
        )
        if values.get(name, "")
    )
    return replace(
        record,
        channel_definition=definition,
        captured_length=integers.get("frame.cap_len"),
        original_length=integers.get("frame.len"),
        radio_frequency_mhz=radio_frequency,
        phy_bandwidth=(";".join(phy_bandwidth_parts) or None),
        fcs_status=integers.get("wlan.fcs.status"),
        sequence_number=integers.get("wlan.seq"),
        fragment_number=integers.get("wlan.frag"),
        qos_tid=integers.get("wlan.qos.tid"),
        ampdu_reference=integers.get("radiotap.ampdu.reference"),
    )


def _band_from_frequency(frequency_mhz: Optional[int]) -> Optional[str]:
    if frequency_mhz is None:
        return None
    if 2400 <= frequency_mhz <= 2500:
        return "2.4"
    if 5000 < frequency_mhz < 5925:
        return "5"
    if 5925 <= frequency_mhz <= 7125:
        return "6"
    return None


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
        parsed = int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return _MALFORMED

    if minimum is not None and parsed < minimum:
        return _MALFORMED
    if maximum is not None and parsed > maximum:
        return _MALFORMED
    return parsed
