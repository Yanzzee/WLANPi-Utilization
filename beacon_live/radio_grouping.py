"""Deterministic, best-effort grouping of beaconing BSSIDs into radios."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from beacon_live.models import BssidState

DEFAULT_RADIO_RSSI_TOLERANCE_DB = 3
AP_NAME_RSSI_LIMIT_DB = 10


@dataclass(frozen=True)
class EstimatedRadio:
    """One analyzer-internal estimated physical radio."""

    group_id: str
    members: tuple[BssidState, ...]
    best_rssi_dbm: Optional[int]
    ap_name: Optional[str]
    vendor: Optional[str]

    @property
    def bssids(self) -> tuple[str, ...]:
        return tuple(member.bssid for member in self.members)


def estimate_radio_groups(
    states: tuple[BssidState, ...],
    *,
    rssi_tolerance_db: int = DEFAULT_RADIO_RSSI_TOLERANCE_DB,
) -> tuple[EstimatedRadio, ...]:
    """Group states conservatively using complete-link compatibility.

    Requiring a new BSSID to match every member avoids a transitive chain of
    individually similar BSSIDs collapsing radios whose endpoint RSSIs or AP
    names clearly disagree.
    """
    if rssi_tolerance_db < 0:
        raise ValueError("rssi_tolerance_db must not be negative")

    member_groups: list[list[BssidState]] = []
    for state in sorted(states, key=lambda item: item.bssid):
        compatible_group = next(
            (
                group
                for group in member_groups
                if all(
                    likely_same_radio(
                        state,
                        member,
                        rssi_tolerance_db=rssi_tolerance_db,
                    )
                    for member in group
                )
            ),
            None,
        )
        if compatible_group is None:
            member_groups.append([state])
        else:
            compatible_group.append(state)

    return tuple(_estimated_radio(tuple(group)) for group in member_groups)


def likely_same_radio(
    first: BssidState,
    second: BssidState,
    *,
    rssi_tolerance_db: int = DEFAULT_RADIO_RSSI_TOLERANCE_DB,
) -> bool:
    """Return whether two BSSIDs satisfy the Phase 4 radio heuristics."""
    if first.bssid == second.bssid:
        return True

    first_oui = vendor_oui(first.bssid)
    second_oui = vendor_oui(second.bssid)
    if first_oui is None or first_oui != second_oui:
        return False

    first_ap_name = _normalized_text(first.latest_ap_name)
    second_ap_name = _normalized_text(second.latest_ap_name)
    if (
        first_ap_name is not None
        and second_ap_name is not None
        and first_ap_name != second_ap_name
    ):
        return False

    rssi_difference = _rssi_difference(first, second)
    if first_ap_name is not None and first_ap_name == second_ap_name:
        return (
            rssi_difference is None
            or rssi_difference <= AP_NAME_RSSI_LIMIT_DB
        )

    return (
        rssi_difference is not None
        and rssi_difference <= rssi_tolerance_db
        and related_bssid_macs(first.bssid, second.bssid)
    )


def select_strongest_radio(
    groups: tuple[EstimatedRadio, ...],
    current_bssids: tuple[str, ...] = (),
) -> Optional[EstimatedRadio]:
    """Select the strongest group without using beacon timing."""
    if not groups:
        return None

    with_rssi = tuple(
        group for group in groups if group.best_rssi_dbm is not None
    )
    if with_rssi:
        strongest_rssi = max(
            group.best_rssi_dbm
            for group in with_rssi
            if group.best_rssi_dbm is not None
        )
        finalists = tuple(
            group
            for group in with_rssi
            if group.best_rssi_dbm == strongest_rssi
        )
    else:
        finalists = groups

    current_set = set(current_bssids)
    if current_set:
        retained = tuple(
            group for group in finalists if current_set.intersection(group.bssids)
        )
        if retained:
            return min(retained, key=lambda group: group.group_id)

    return min(finalists, key=lambda group: group.group_id)


def vendor_oui(bssid: str) -> Optional[str]:
    """Return the normalized first three MAC octets, if the BSSID is valid."""
    compact = bssid.replace(":", "").replace("-", "").lower()
    if len(compact) != 12 or any(character not in "0123456789abcdef" for character in compact):
        return None
    return compact[:6]


def related_bssid_macs(first: str, second: str) -> bool:
    """Return whether MACs are adjacent or differ in one hexadecimal digit."""
    first_compact = _normalized_mac(first)
    second_compact = _normalized_mac(second)
    if first_compact is None or second_compact is None:
        return False
    if abs(int(first_compact, 16) - int(second_compact, 16)) == 1:
        return True
    return sum(
        first_digit != second_digit
        for first_digit, second_digit in zip(first_compact, second_compact)
    ) == 1


def _estimated_radio(members: tuple[BssidState, ...]) -> EstimatedRadio:
    ordered_members = tuple(sorted(members, key=lambda member: member.bssid))
    strongest_first = tuple(sorted(ordered_members, key=_member_strength_key))
    rssi_values = tuple(
        member.peak_rssi_dbm
        for member in ordered_members
        if member.peak_rssi_dbm is not None
    )
    return EstimatedRadio(
        group_id=ordered_members[0].bssid,
        members=ordered_members,
        best_rssi_dbm=max(rssi_values) if rssi_values else None,
        ap_name=next(
            (
                member.latest_ap_name
                for member in strongest_first
                if member.latest_ap_name
            ),
            None,
        ),
        vendor=next(
            (
                member.latest_vendor
                for member in strongest_first
                if member.latest_vendor
            ),
            None,
        ),
    )


def _member_strength_key(state: BssidState) -> tuple[int, str]:
    return (
        -(state.peak_rssi_dbm if state.peak_rssi_dbm is not None else -200),
        state.bssid,
    )


def _rssi_difference(first: BssidState, second: BssidState) -> Optional[int]:
    if first.peak_rssi_dbm is None or second.peak_rssi_dbm is None:
        return None
    return abs(first.peak_rssi_dbm - second.peak_rssi_dbm)


def _normalized_text(value: Optional[str]) -> Optional[str]:
    if value is None or not value.strip():
        return None
    return value.strip().casefold()


def _normalized_mac(value: str) -> Optional[str]:
    compact = value.replace(":", "").replace("-", "").lower()
    if len(compact) != 12:
        return None
    if any(character not in "0123456789abcdef" for character in compact):
        return None
    return compact
