"""UI-only screen registry and debounced navigation state."""

from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

from beacon_live.models import MetricsSnapshot
from beacon_live.screens import DEFAULT_SCREENS
from beacon_live.screens import ScreenDefinition
from beacon_live.screens import ScreenView

DEFAULT_NAVIGATION_DEBOUNCE_SECONDS = 0.2


class ScreenManager:
    """Select one stateless renderer without owning analyzer history."""

    def __init__(
        self,
        *,
        screens: Sequence[ScreenDefinition] = DEFAULT_SCREENS,
        snapshot: Optional[MetricsSnapshot] = None,
        debounce_seconds: float = DEFAULT_NAVIGATION_DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not screens:
            raise ValueError("screens must not be empty")
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must not be negative")
        self._screens = tuple(screens)
        self._snapshot = snapshot or MetricsSnapshot.empty()
        self._active_index = 0
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        self._last_navigation_ts: Optional[float] = None

    @property
    def snapshot(self) -> MetricsSnapshot:
        return self._snapshot

    @property
    def screen_count(self) -> int:
        return len(self._screens)

    @property
    def active_index(self) -> int:
        return self._active_index

    @property
    def active_screen_id(self) -> str:
        return self._screens[self._active_index].screen_id

    @property
    def view(self) -> ScreenView:
        return self._screens[self._active_index].render(self._snapshot)

    def update(self, snapshot: MetricsSnapshot) -> None:
        self._snapshot = snapshot

    def set_active_index(self, index: int) -> bool:
        new_index = index % self.screen_count
        changed = new_index != self._active_index
        self._active_index = new_index
        return changed

    def navigate_up(self, *, now: Optional[float] = None) -> bool:
        return self._navigate(-1, now=now)

    def navigate_down(self, *, now: Optional[float] = None) -> bool:
        return self._navigate(1, now=now)

    def _navigate(self, offset: int, *, now: Optional[float]) -> bool:
        timestamp = self._clock() if now is None else now
        if (
            self._last_navigation_ts is not None
            and timestamp - self._last_navigation_ts < self._debounce_seconds
        ):
            return False
        self._last_navigation_ts = timestamp
        return self.set_active_index(self._active_index + offset)
