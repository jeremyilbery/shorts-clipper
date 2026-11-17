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

# Long-form 16:9 clip for a regular YouTube upload: 8–19 min (always under 20), normal
# speed. The AI picks the length the conversation needs inside that range.
LONG = ClipProfile(kind="long", min_seconds=480.0, max_seconds=1140.0, pacing=1.0, layout="landscape")

# Source videos that are already clips (e.g. JRE Clips): keep nearly the whole video.
# Any length up to the long-form cap; longer videos get their best 15–19 min.
FULL = ClipProfile(kind="long", min_seconds=60.0, max_seconds=1140.0, pacing=1.0, layout="landscape")
FULL_TRIM_MIN_S = 900.0


def full_video_window(segments, source_duration: float | None) -> ClipWindow | None:
    """Nearly the whole video: first spoken word to last, or None if it's over the cap.

    Uses the subtitle timings when they cover the video; otherwise the known duration.
    """
    last_end = segments[-1].end if segments else 0.0
    duration = source_duration or last_end
    if not duration or duration > FULL.source_max:
        return None
    if segments and last_end >= 0.8 * duration:
        start = min(segments[0].start, 30.0)
        end = min(duration, last_end + 1.0) if source_duration else last_end + 1.0
    else:
        start, end = 0.0, duration
    return ClipWindow(start=max(0.0, start), end=end)


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
