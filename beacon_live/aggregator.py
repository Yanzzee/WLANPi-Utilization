"""Compatibility helpers for finite per-second beacon aggregation."""

from typing import Iterable, Optional

from beacon_live.analyzer import Analyzer
from beacon_live.models import BeaconRecord
from beacon_live.models import SecondStats


class Aggregator(Analyzer):
    """Backward-compatible name for the shared rolling analyzer."""


def aggregate_records(
    records: Iterable[BeaconRecord],
    *,
    local_cu_by_second: Optional[dict[int, Optional[float]]] = None,
) -> list[SecondStats]:
    """Analyze a finite iterable and return every observed per-second sample."""
    analyzer = Analyzer()
    for second, percent in (local_cu_by_second or {}).items():
        analyzer.set_local_cu_percent(second, percent)

    stats: list[SecondStats] = []
    for record in records:
        stats.extend(analyzer.add(record))
    stats.extend(analyzer.flush())
    return stats
