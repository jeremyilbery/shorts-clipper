"""Clip profiles: the length, pacing and framing for each kind of clip the pipeline makes."""

from __future__ import annotations

from dataclasses import dataclass

from shorts_clipper.core.models import ClipWindow


@dataclass(frozen=True)
class ClipProfile:
    """How one kind of clip is cut and rendered.

    ``min_seconds`` / ``max_seconds`` are the length of the *finished* clip, after
    ``pacing`` (the speed-up) is applied. Source windows are converted with
    ``source_min`` / ``source_max``.
    """

    kind: str
    min_seconds: float
    max_seconds: float
    pacing: float
    layout: str

    @property
    def source_min(self) -> float:
        return self.min_seconds * self.pacing

    @property
    def source_max(self) -> float:
        return self.max_seconds * self.pacing

    @property
    def landscape(self) -> bool:
        return self.layout == "landscape"


# Vertical 9:16 short for YouTube Shorts / Reels: 20–60 s finished, sped up 1.15×.
SHORT = ClipProfile(kind="short", min_seconds=20.0, max_seconds=60.0, pacing=1.15, layout="center")

# Long-form 16:9 clip for a regular YouTube upload: 8–12 min, normal speed.
LONG = ClipProfile(kind="long", min_seconds=480.0, max_seconds=720.0, pacing=1.0, layout="landscape")


def fit_window(window: ClipWindow, profile: ClipProfile) -> ClipWindow:
    """Stretch or trim a source window so it fits the profile's length range.

    Too short: the end is pushed out to the minimum. Too long: the end is pulled in to the maximum.
    The start is kept, since that is where the AI judged the moment begins.
    """
    duration = window.end - window.start
    if duration < profile.source_min:
        return ClipWindow(start=window.start, end=window.start + profile.source_min)
    if duration > profile.source_max:
        return ClipWindow(start=window.start, end=window.start + profile.source_max)
    return window
