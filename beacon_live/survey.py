"""Parsing and math for saved ``iw dev <iface> survey dump`` output."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from beacon_live.models import SurveySample

_SURVEY_HEADER_RE = re.compile(r"^\s*Survey data from\b")
_IN_USE_RE = re.compile(r"^\s*in use\s*$")
_FREQUENCY_RE = re.compile(
    r"^\s*(?:frequency:\s*|channel\s+\d+\s+\()(\d+)\s*MHz\b"
)
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


@dataclass(frozen=True)
class SurveyCuResult:
    local_cu_percent: Optional[float]
    frequency_mhz: Optional[int]
    active_delta_ms: Optional[int]
    busy_delta_ms: Optional[int]
    reason: str


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

        if _IN_USE_RE.match(line):
            current["in_use"] = True
            continue

        frequency_match = _FREQUENCY_RE.match(line)
        if frequency_match:
            current["frequency_mhz"] = int(frequency_match.group(1))
            continue

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


def compute_local_cu_percent_from_samples(
    previous_samples: Iterable[SurveySample],
    current_samples: Iterable[SurveySample],
    *,
    target_frequency_mhz: Optional[int] = None,
) -> Optional[float]:
    """Compute one local CU value from paired survey dumps."""
    result = compute_local_cu_result_from_samples(
        previous_samples,
        current_samples,
        target_frequency_mhz=target_frequency_mhz,
    )
    return result.local_cu_percent


def compute_local_cu_result_from_samples(
    previous_samples: Iterable[SurveySample],
    current_samples: Iterable[SurveySample],
    *,
    target_frequency_mhz: Optional[int] = None,
) -> SurveyCuResult:
    """Compute one local CU value from paired survey dumps.

    ``iw survey dump`` can include many channels. Prefer the caller's target
    frequency when available, then the ``in use`` survey entry, then the valid
    before/after pair with the largest active-time delta.
    """
    pairs = _paired_samples(list(previous_samples), list(current_samples))
    if not pairs:
        return _empty_result("no paired survey samples")

    if target_frequency_mhz is not None:
        target_pairs = [
            pair
            for pair in pairs
            if pair[0].frequency_mhz == target_frequency_mhz
            or pair[1].frequency_mhz == target_frequency_mhz
        ]
        if not target_pairs:
            return _empty_result(
                f"target frequency {target_frequency_mhz} MHz not present"
            )
        return _best_valid_result(
            target_pairs,
            f"target frequency {target_frequency_mhz} MHz counters unavailable",
        )

    in_use_pairs = [
        pair for pair in pairs if pair[0].in_use or pair[1].in_use
    ]
    if in_use_pairs:
        return _best_valid_result(
            in_use_pairs,
            "in-use survey counters unavailable",
        )

    return _best_valid_result(pairs, "no valid survey counter deltas")


def _paired_samples(
    previous_samples: list[SurveySample],
    current_samples: list[SurveySample],
) -> list[tuple[SurveySample, SurveySample]]:
    previous_by_frequency = {
        sample.frequency_mhz: sample
        for sample in previous_samples
        if sample.frequency_mhz is not None
    }
    current_frequencies = [
        sample.frequency_mhz
        for sample in current_samples
        if sample.frequency_mhz is not None
    ]

    if previous_by_frequency and current_frequencies:
        pairs = [
            (previous_by_frequency[current.frequency_mhz], current)
            for current in current_samples
            if current.frequency_mhz in previous_by_frequency
        ]
        if pairs:
            return pairs

    return list(zip(previous_samples, current_samples))


def _best_valid_result(
    pairs: Iterable[tuple[SurveySample, SurveySample]],
    unavailable_reason: str,
) -> SurveyCuResult:
    best_result: Optional[SurveyCuResult] = None

    for previous, current in pairs:
        if (
            previous.active_ms is None
            or previous.busy_ms is None
            or current.active_ms is None
            or current.busy_ms is None
        ):
            continue

        active_delta = current.active_ms - previous.active_ms
        busy_delta = current.busy_ms - previous.busy_ms
        if active_delta <= 0 or busy_delta < 0:
            continue

        result = SurveyCuResult(
            local_cu_percent=busy_delta / active_delta * 100,
            frequency_mhz=current.frequency_mhz or previous.frequency_mhz,
            active_delta_ms=active_delta,
            busy_delta_ms=busy_delta,
            reason="ok",
        )
        if (
            best_result is None
            or result.active_delta_ms is not None
            and best_result.active_delta_ms is not None
            and result.active_delta_ms > best_result.active_delta_ms
        ):
            best_result = result

    if best_result is not None:
        return best_result

    return _empty_result(unavailable_reason)


def _empty_result(reason: str) -> SurveyCuResult:
    return SurveyCuResult(
        local_cu_percent=None,
        frequency_mhz=None,
        active_delta_ms=None,
        busy_delta_ms=None,
        reason=reason,
    )


def _empty_fields() -> dict[str, object]:
    return {
        "active_ms": None,
        "busy_ms": None,
        "receive_ms": None,
        "transmit_ms": None,
        "noise_dbm": None,
        "frequency_mhz": None,
        "in_use": False,
    }


def _append_sample(
    samples: list[SurveySample],
    timestamp: float,
    fields: dict[str, object],
) -> None:
    has_counter_or_frequency = any(
        fields[field_name] is not None
        for field_name in (
            "active_ms",
            "busy_ms",
            "receive_ms",
            "transmit_ms",
            "noise_dbm",
            "frequency_mhz",
        )
    )
    if has_counter_or_frequency or fields["in_use"] is True:
        samples.append(
            SurveySample(
                timestamp=timestamp,
                active_ms=fields["active_ms"],
                busy_ms=fields["busy_ms"],
                receive_ms=fields["receive_ms"],
                transmit_ms=fields["transmit_ms"],
                noise_dbm=fields["noise_dbm"],
                frequency_mhz=fields["frequency_mhz"],
                in_use=bool(fields["in_use"]),
            )
        )
