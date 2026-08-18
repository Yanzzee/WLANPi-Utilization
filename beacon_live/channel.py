"""Pure 802.11 channel-definition parsing, coverage, and retune policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class ChannelWidth(str, Enum):
    MHZ20 = "20"
    MHZ40 = "40"
    MHZ80 = "80"
    MHZ160 = "160"
    MHZ80P80 = "80+80"
    MHZ320 = "320"

    @property
    def mhz(self) -> int:
        return 160 if self is ChannelWidth.MHZ80P80 else int(self.value)


CONTIGUOUS_WIDTHS = (
    ChannelWidth.MHZ20,
    ChannelWidth.MHZ40,
    ChannelWidth.MHZ80,
    ChannelWidth.MHZ160,
    ChannelWidth.MHZ320,
)


@dataclass(frozen=True)
class ChannelDefinition:
    """One Linux/nl80211-style operating channel definition."""

    primary_frequency_mhz: int
    width: ChannelWidth = ChannelWidth.MHZ20
    center_frequency1_mhz: Optional[int] = None
    center_frequency2_mhz: Optional[int] = None
    primary_channel: Optional[int] = None
    phy: str = "unknown"
    puncturing_bitmap: Optional[int] = None
    complete: bool = True
    supported: bool = True
    ambiguous: bool = False
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.primary_frequency_mhz <= 0:
            raise ValueError("primary_frequency_mhz must be greater than zero")
        if self.center_frequency1_mhz is None:
            object.__setattr__(
                self,
                "center_frequency1_mhz",
                self.primary_frequency_mhz,
            )
        if self.width is ChannelWidth.MHZ80P80 and self.center_frequency2_mhz is None:
            object.__setattr__(self, "complete", False)
            object.__setattr__(self, "ambiguous", True)

    @property
    def label(self) -> str:
        centers = str(self.center_frequency1_mhz)
        if self.center_frequency2_mhz is not None:
            centers += f"/{self.center_frequency2_mhz}"
        puncturing = (
            "" if self.puncturing_bitmap is None else f" punct=0x{self.puncturing_bitmap:x}"
        )
        return (
            f"{self.primary_frequency_mhz} MHz {self.width.value} MHz "
            f"center={centers}{puncturing}"
        )

    @property
    def active_20mhz_centers(self) -> frozenset[int]:
        centers: set[int] = set()
        if self.width is ChannelWidth.MHZ80P80:
            for center in (self.center_frequency1_mhz, self.center_frequency2_mhz):
                if center is not None:
                    centers.update(_continuous_centers(center, 80))
        elif self.center_frequency1_mhz is not None:
            centers.update(_continuous_centers(self.center_frequency1_mhz, self.width.mhz))
        if self.puncturing_bitmap is not None:
            ordered = sorted(centers)
            centers = {
                center
                for index, center in enumerate(ordered)
                if not self.puncturing_bitmap & (1 << index)
            }
        return frozenset(centers)

    def contains(self, other: "ChannelDefinition") -> bool:
        return (
            self.primary_frequency_mhz == other.primary_frequency_mhz
            and self.active_20mhz_centers.issuperset(other.active_20mhz_centers)
        )

    def same_tuning(self, other: "ChannelDefinition") -> bool:
        return (
            self.primary_frequency_mhz == other.primary_frequency_mhz
            and self.width is other.width
            and self.center_frequency1_mhz == other.center_frequency1_mhz
            and self.center_frequency2_mhz == other.center_frequency2_mhz
            and self.puncturing_bitmap == other.puncturing_bitmap
        )


@dataclass(frozen=True)
class BssidChannelObservation:
    bssid: str
    definition: ChannelDefinition
    observed_at: float


@dataclass(frozen=True)
class RadioCapabilities:
    """Conservative capabilities inferred from ``iw phy`` output."""

    supported_widths: frozenset[ChannelWidth]
    supported_frequencies_mhz: frozenset[int] = frozenset()
    supports_monitor: bool = True
    supports_puncturing: bool = False
    source_complete: bool = False

    @classmethod
    def ht20_only(cls) -> "RadioCapabilities":
        return cls(frozenset({ChannelWidth.MHZ20}), source_complete=False)

    def supports(self, definition: ChannelDefinition) -> tuple[bool, str]:
        if not definition.complete or definition.ambiguous:
            return False, definition.reason or "channel definition is incomplete"
        if not definition.supported:
            return False, definition.reason or "channel definition is not portable"
        if not self.supports_monitor:
            return False, "phy does not advertise monitor mode"
        if definition.width not in self.supported_widths:
            return False, f"phy does not advertise {definition.width.value} MHz"
        if (
            self.supported_frequencies_mhz
            and not definition.active_20mhz_centers.issubset(
                self.supported_frequencies_mhz
            )
        ):
            return False, "one or more 20 MHz segments are disabled or absent from iw phy"
        if definition.puncturing_bitmap is not None and not self.supports_puncturing:
            return False, "iw/driver puncturing support was not detected"
        return True, "supported"


@dataclass(frozen=True)
class CoverageDecision:
    requested: ChannelDefinition
    covered_bssids: tuple[str, ...]
    partial_bssids: tuple[str, ...]
    status: str
    warning: Optional[str] = None

    @property
    def complete(self) -> bool:
        return not self.partial_bssids and self.status == "complete"


def frequency_to_channel(frequency_mhz: int) -> Optional[int]:
    if frequency_mhz == 2484:
        return 14
    if 2412 <= frequency_mhz <= 2472 and (frequency_mhz - 2407) % 5 == 0:
        return (frequency_mhz - 2407) // 5
    if 5005 <= frequency_mhz < 5925 and (frequency_mhz - 5000) % 5 == 0:
        return (frequency_mhz - 5000) // 5
    if 5925 <= frequency_mhz <= 7125 and (frequency_mhz - 5950) % 5 == 0:
        return (frequency_mhz - 5950) // 5
    return None


def channel_to_frequency(channel: int, band: str) -> Optional[int]:
    if band == "2.4":
        if channel == 14:
            return 2484
        return 2407 + channel * 5 if 1 <= channel <= 13 else None
    if band == "5":
        frequency = 5000 + channel * 5
        return frequency if 5000 < frequency < 5925 else None
    if band == "6":
        frequency = 5950 + channel * 5
        return frequency if 5925 <= frequency <= 7125 else None
    return None


def parse_channel_width(value: str) -> ChannelWidth:
    normalized = value.strip().lower().replace("mhz", "").replace(" ", "")
    aliases = {
        "20": ChannelWidth.MHZ20,
        "ht20": ChannelWidth.MHZ20,
        "40": ChannelWidth.MHZ40,
        "ht40+": ChannelWidth.MHZ40,
        "ht40-": ChannelWidth.MHZ40,
        "80": ChannelWidth.MHZ80,
        "160": ChannelWidth.MHZ160,
        "80+80": ChannelWidth.MHZ80P80,
        "80p80": ChannelWidth.MHZ80P80,
        "320": ChannelWidth.MHZ320,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported channel width {value!r}") from exc


def definition_from_operation_fields(
    *,
    band: str,
    fallback_primary_frequency_mhz: Optional[int] = None,
    ds_primary_channel: Optional[int] = None,
    ht_primary_channel: Optional[int] = None,
    ht_secondary_offset: Optional[int] = None,
    vht_width: Optional[int] = None,
    vht_center0: Optional[int] = None,
    vht_center1: Optional[int] = None,
    he_primary_channel: Optional[int] = None,
    he_width: Optional[int] = None,
    he_center0: Optional[int] = None,
    he_center1: Optional[int] = None,
    eht_width: Optional[int] = None,
    eht_center0: Optional[int] = None,
    eht_center1: Optional[int] = None,
    puncturing_bitmap: Optional[int] = None,
) -> Optional[ChannelDefinition]:
    """Resolve HT/VHT/HE/EHT operation fields without guessing from primary."""
    if puncturing_bitmap == 0:
        puncturing_bitmap = None
    primary_channel = he_primary_channel or ht_primary_channel or ds_primary_channel
    primary_frequency = (
        channel_to_frequency(primary_channel, band)
        if primary_channel is not None
        else fallback_primary_frequency_mhz
    )
    if primary_frequency is None:
        return None
    operation_primary_channel = (
        primary_channel
        if primary_channel is not None
        else frequency_to_channel(primary_frequency)
    )

    if eht_width is not None:
        width = {
            0: ChannelWidth.MHZ20,
            1: ChannelWidth.MHZ40,
            2: ChannelWidth.MHZ80,
            3: ChannelWidth.MHZ160,
            4: ChannelWidth.MHZ320,
        }.get(eht_width)
        if width is None:
            return _ambiguous_definition(
                primary_frequency, primary_channel, "eht", "unknown EHT width code"
            )
        center1 = _segment_frequency(eht_center0, band)
        center2 = _segment_frequency(eht_center1, band)
        return _operation_definition(
            primary_frequency,
            operation_primary_channel,
            width,
            center1,
            center2,
            "eht",
            puncturing_bitmap=puncturing_bitmap,
        )

    if he_width is not None:
        width = {
            0: ChannelWidth.MHZ20,
            1: ChannelWidth.MHZ40,
            2: ChannelWidth.MHZ80,
            3: ChannelWidth.MHZ160,
        }.get(he_width)
        if width is None:
            return _ambiguous_definition(
                primary_frequency, primary_channel, "he", "unknown HE 6 GHz width code"
            )
        if width is ChannelWidth.MHZ160 and he_center1:
            separation = (
                abs(he_center1 - he_center0)
                if he_center0 is not None
                else None
            )
            if separation == 8:
                pass
            elif separation is not None and separation > 16:
                width = ChannelWidth.MHZ80P80
            else:
                return _ambiguous_definition(
                    primary_frequency,
                    primary_channel,
                    "he",
                    "HE center segments do not identify 160 or 80+80 MHz",
                )
        center1 = _segment_frequency(
            he_center1 if width is ChannelWidth.MHZ160 else he_center0,
            band,
        )
        center2 = (
            _segment_frequency(he_center1, band)
            if width is ChannelWidth.MHZ80P80
            else None
        )
        return _operation_definition(
            primary_frequency,
            operation_primary_channel,
            width,
            center1,
            center2,
            "he",
        )

    if vht_width is not None and vht_width != 0:
        if vht_width == 1:
            if vht_center1 in (None, 0):
                width = ChannelWidth.MHZ80
                center1_channel = vht_center0
                center2_channel = None
            elif vht_center0 is not None:
                separation = abs(vht_center1 - vht_center0)
                if separation == 8:
                    width = ChannelWidth.MHZ160
                    center1_channel = vht_center1
                    center2_channel = None
                elif separation > 16:
                    width = ChannelWidth.MHZ80P80
                    center1_channel = vht_center0
                    center2_channel = vht_center1
                else:
                    return _ambiguous_definition(
                        primary_frequency,
                        primary_channel,
                        "vht",
                        "VHT center segments do not identify 160 or 80+80 MHz",
                    )
            else:
                return _ambiguous_definition(
                    primary_frequency,
                    primary_channel,
                    "vht",
                    "VHT center segment 0 is missing",
                )
        elif vht_width == 2:
            width = ChannelWidth.MHZ160
            center1_channel = vht_center0
            center2_channel = None
        elif vht_width == 3:
            width = ChannelWidth.MHZ80P80
            center1_channel = vht_center0
            center2_channel = vht_center1
        else:
            return _ambiguous_definition(
                primary_frequency, primary_channel, "vht", "unknown VHT width code"
            )
        return _operation_definition(
            primary_frequency,
            operation_primary_channel,
            width,
            _segment_frequency(center1_channel, band),
            _segment_frequency(center2_channel, band),
            "vht",
        )

    if ht_secondary_offset in {1, 3}:
        center = primary_frequency + (10 if ht_secondary_offset == 1 else -10)
        return ChannelDefinition(
            primary_frequency,
            ChannelWidth.MHZ40,
            center,
            primary_channel=primary_channel,
            phy="ht",
        )
    if ht_secondary_offset not in (None, 0):
        return _ambiguous_definition(
            primary_frequency, primary_channel, "ht", "reserved HT secondary offset"
        )
    inferred_from_radio = (
        primary_channel is None
        and fallback_primary_frequency_mhz is not None
    )
    definition = ChannelDefinition(
        primary_frequency,
        ChannelWidth.MHZ20,
        primary_frequency,
        primary_channel=primary_channel,
        phy=(
            "ht"
            if ht_primary_channel is not None
            else "radio-frequency"
            if inferred_from_radio
            else "legacy"
        ),
        complete=primary_channel is not None,
        ambiguous=primary_channel is None,
        reason=(
            None
            if primary_channel is not None
            else "operation fields unavailable; primary inferred from capture frequency"
        ),
    )
    return definition


def parse_iw_phy_capabilities(text: str) -> RadioCapabilities:
    lower = text.lower()
    widths = {ChannelWidth.MHZ20}
    if "ht capabilities" in lower or "ht20/ht40" in lower or "ht40" in lower:
        widths.add(ChannelWidth.MHZ40)
    if "vht capabilities" in lower or "he iftypes" in lower or "eht iftypes" in lower:
        widths.add(ChannelWidth.MHZ80)
    if "160 mhz" in lower or "160mhz" in lower or "he160" in lower:
        widths.add(ChannelWidth.MHZ160)
    if "80+80" in lower or "80p80" in lower:
        widths.add(ChannelWidth.MHZ80P80)
    if "320 mhz" in lower or "320mhz" in lower:
        widths.add(ChannelWidth.MHZ320)
    frequencies = frozenset(
        int(value)
        for value in re.findall(r"\*\s+(\d{4})\s+mhz", lower)
        if "disabled" not in _line_containing(lower, value)
    )
    supports_monitor = bool(re.search(r"^\s*\*\s+monitor\s*$", lower, re.MULTILINE))
    return RadioCapabilities(
        supported_widths=frozenset(widths),
        supported_frequencies_mhz=frequencies,
        supports_monitor=supports_monitor,
        # This is iw's rendering of NL80211_EXT_FEATURE_PUNCT. Generic HE
        # "Punctured Preamble RX" capabilities do not mean nl80211 will
        # accept a punctured channel definition.
        supports_puncturing="preamble puncturing in ap mode" in lower,
        source_complete=bool(text.strip()),
    )


def parse_iw_interface_channel(text: str) -> Optional[ChannelDefinition]:
    match = re.search(
        r"channel\s+(\d+)\s+\((\d+)\s+mhz\),\s*width:\s*"
        r"(20|40|80\+80|80|160|320)\s+mhz(?:\s*\([^)]*\))?,\s*"
        r"center1:\s*(\d+)\s+mhz(?:,\s*center2:\s*(\d+)\s+mhz)?"
        r"(?:,\s*punctured:\s*(0x[0-9a-f]+|\d+))?",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    channel, primary, width, center1, center2, puncturing = match.groups()
    return ChannelDefinition(
        primary_frequency_mhz=int(primary),
        width=parse_channel_width(width),
        center_frequency1_mhz=int(center1),
        center_frequency2_mhz=int(center2) if center2 else None,
        primary_channel=int(channel),
        phy="actual",
        puncturing_bitmap=(int(puncturing, 0) if puncturing else None),
    )


def choose_capture_definition(
    observations: Iterable[BssidChannelObservation],
    *,
    target_primary_frequency_mhz: int,
    capabilities: RadioCapabilities,
    now: float,
    max_age_seconds: float,
) -> CoverageDecision:
    """Choose one stable, widest compatible definition without hopping."""
    latest: dict[str, BssidChannelObservation] = {}
    for observation in observations:
        if observation.observed_at > now or now - observation.observed_at > max_age_seconds:
            continue
        if observation.definition.primary_frequency_mhz != target_primary_frequency_mhz:
            continue
        previous = latest.get(observation.bssid)
        if previous is None or observation.observed_at >= previous.observed_at:
            latest[observation.bssid] = observation

    fallback = ChannelDefinition(
        target_primary_frequency_mhz,
        ChannelWidth.MHZ20,
        target_primary_frequency_mhz,
        primary_channel=frequency_to_channel(target_primary_frequency_mhz),
        phy="fallback",
    )
    if not latest:
        return CoverageDecision(fallback, (), (), "fallback", "no fresh complete channel definitions; using HT20")

    definitions = tuple(observation.definition for observation in latest.values())
    candidates = sorted(
        (definition for definition in definitions if definition.complete and not definition.ambiguous),
        key=lambda definition: (len(definition.active_20mhz_centers), definition.width.mhz),
        reverse=True,
    )
    supported_candidates = [
        candidate for candidate in candidates if capabilities.supports(candidate)[0]
    ]
    for candidate in supported_candidates:
        if all(
            definition.complete
            and definition.supported
            and not definition.ambiguous
            and capabilities.supports(definition)[0]
            and candidate.contains(definition)
            for definition in definitions
        ):
            return CoverageDecision(candidate, tuple(sorted(latest)), (), "complete")

    scoring: list[tuple[int, int, ChannelDefinition, tuple[str, ...]]] = []
    for candidate in supported_candidates:
        covered = tuple(
            sorted(
                bssid
                for bssid, observation in latest.items()
                if observation.definition.complete
                and observation.definition.supported
                and not observation.definition.ambiguous
                and capabilities.supports(observation.definition)[0]
                and candidate.contains(observation.definition)
            )
        )
        scoring.append((len(covered), len(candidate.active_20mhz_centers), candidate, covered))
    if scoring:
        _, _, selected, covered = max(scoring, key=lambda item: (item[0], item[1]))
    else:
        selected, covered = fallback, tuple(
            sorted(
                bssid
                for bssid, observation in latest.items()
                if observation.definition.complete
                and observation.definition.supported
                and not observation.definition.ambiguous
                and capabilities.supports(observation.definition)[0]
                and fallback.contains(observation.definition)
            )
        )
    partial = tuple(sorted(set(latest) - set(covered)))
    reasons = sorted(
        {
            capabilities.supports(definition)[1]
            for definition in definitions
            if not capabilities.supports(definition)[0]
        }
        | {
            definition.reason or "ambiguous channel definition"
            for definition in definitions
            if not definition.complete or definition.ambiguous
        }
    )
    warning = (
        "partial bonded-channel coverage; "
        + ("; ".join(reasons) if reasons else "definitions conflict in one hardware channel")
    )
    return CoverageDecision(selected, covered, partial or tuple(sorted(latest)), "partial", warning)


@dataclass
class RetuneHysteresis:
    """Require a candidate to remain stable before changing the radio."""

    stability_seconds: float = 2.0
    minimum_observations: int = 3
    _candidate: Optional[ChannelDefinition] = None
    _candidate_since: Optional[float] = None
    _observations: int = 0

    def consider(
        self,
        requested: ChannelDefinition,
        actual: ChannelDefinition,
        *,
        now: float,
    ) -> bool:
        if requested.same_tuning(actual):
            self.reset()
            return False
        if requested != self._candidate:
            self._candidate = requested
            self._candidate_since = now
            self._observations = 1
            return False
        self._observations += 1
        stable_for = now - (self._candidate_since if self._candidate_since is not None else now)
        if self._observations >= self.minimum_observations and stable_for >= self.stability_seconds:
            self.reset()
            return True
        return False

    def reset(self) -> None:
        self._candidate = None
        self._candidate_since = None
        self._observations = 0


def _operation_definition(
    primary_frequency: int,
    primary_channel: Optional[int],
    width: ChannelWidth,
    center1: Optional[int],
    center2: Optional[int],
    phy: str,
    *,
    puncturing_bitmap: Optional[int] = None,
    unsupported_reason: Optional[str] = None,
) -> ChannelDefinition:
    complete = center1 is not None and (
        width is not ChannelWidth.MHZ80P80 or center2 is not None
    )
    reason = unsupported_reason or (None if complete else "operation center frequency is missing")
    definition = ChannelDefinition(
        primary_frequency,
        width,
        center1 or primary_frequency,
        center2,
        primary_channel,
        phy,
        puncturing_bitmap,
        complete=complete,
        supported=unsupported_reason is None,
        ambiguous=not complete,
        reason=reason,
    )
    if primary_frequency not in definition.active_20mhz_centers:
        return ChannelDefinition(
            primary_frequency,
            width,
            center1 or primary_frequency,
            center2,
            primary_channel,
            phy,
            puncturing_bitmap,
            complete=False,
            supported=False,
            ambiguous=True,
            reason="operation center does not contain the advertised primary",
        )
    return definition


def _ambiguous_definition(
    primary_frequency: int,
    primary_channel: Optional[int],
    phy: str,
    reason: str,
) -> ChannelDefinition:
    return ChannelDefinition(
        primary_frequency,
        ChannelWidth.MHZ20,
        primary_frequency,
        primary_channel=primary_channel,
        phy=phy,
        complete=False,
        supported=False,
        ambiguous=True,
        reason=reason,
    )


def _segment_frequency(segment: Optional[int], band: str) -> Optional[int]:
    if segment in (None, 0):
        return None
    return channel_to_frequency(segment, band)


def _continuous_centers(center: int, width_mhz: int) -> tuple[int, ...]:
    count = max(1, width_mhz // 20)
    first = center - (count - 1) * 10
    return tuple(first + index * 20 for index in range(count))


def _line_containing(text: str, value: str) -> str:
    return next((line for line in text.splitlines() if value in line), "")
