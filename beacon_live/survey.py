"""Parsing and math for saved ``iw dev <iface> survey dump`` output."""

from __future__ import annotations

import re
import time
from typing import Optional

from beacon_live.models import SurveySample

_SURVEY_HEADER_RE = re.compile(r"^\s*Survey data from\b")
_FIELD_PATTERNS = {
    "active_ms": re.compile(r"^\s*(?:channel active time|time):\s*(\d+)\s*ms\b"),
    "busy_ms": re.compile(r"^\s*(?:channel busy time|time busy):\s*(\d+)\s*ms\b"),
    "receive_ms": re.compile(
        r"^\s*(?:channel receive time|time receive):\s*(\d+)\s*ms\b"
    ),
    "transmit_ms": re.compile(
        r"^\s*(?:channel transmit time|time transmit):\s*(\d+)\s*ms\b"
    ),
    "noise_dbm": re.compile(r"^\s*noise:\s*(-?\d+)\s*dBm\b"),
}


def parse_survey_dump(
    text: str,
    *,
    timestamp: Optional[float] = None,
) -> list[SurveySample]:
    """Parse saved survey dump text into one or more survey samples."""
    sample_timestamp = time.time() if timestamp is None else timestamp
    samples: list[SurveySample] = []
    current: Optional[dict[str, Optional[int]]] = None

    for line in text.splitlines():
        if _SURVEY_HEADER_RE.match(line):
            if current is not None:
                _append_sample(samples, sample_timestamp, current)
            current = _empty_fields()
            continue

        if current is None:
            current = _empty_fields()

        for field_name, pattern in _FIELD_PATTERNS.items():
            match = pattern.match(line)
            if match:
                current[field_name] = int(match.group(1))
                break

    if current is not None:
        _append_sample(samples, sample_timestamp, current)

    return samples


def parse_survey_text(
    text: str,
    *,
    timestamp: Optional[float] = None,
) -> list[SurveySample]:
    """Alias for callers that prefer a generic text-oriented name."""
    return parse_survey_dump(text, timestamp=timestamp)


def compute_local_cu_percent(
    previous: SurveySample,
    current: SurveySample,
) -> Optional[float]:
    """Compute local channel utilization from two survey counter samples."""
    if (
        previous.active_ms is None
        or previous.busy_ms is None
        or current.active_ms is None
        or current.busy_ms is None
    ):
        return None

    active_delta = current.active_ms - previous.active_ms
    busy_delta = current.busy_ms - previous.busy_ms

    if active_delta <= 0 or busy_delta < 0:
        return None

    return busy_delta / active_delta * 100


def _empty_fields() -> dict[str, Optional[int]]:
    return {
        "active_ms": None,
        "busy_ms": None,
        "receive_ms": None,
        "transmit_ms": None,
        "noise_dbm": None,
    }


def _append_sample(
    samples: list[SurveySample],
    timestamp: float,
    fields: dict[str, Optional[int]],
) -> None:
    if any(value is not None for value in fields.values()):
        samples.append(
            SurveySample(
                timestamp=timestamp,
                active_ms=fields["active_ms"],
                busy_ms=fields["busy_ms"],
                receive_ms=fields["receive_ms"],
                transmit_ms=fields["transmit_ms"],
                noise_dbm=fields["noise_dbm"],
            )
        )
