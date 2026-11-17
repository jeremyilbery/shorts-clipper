"""Short / long-form clip profiles: length limits, landscape rendering and CLI flags."""

import pytest

from shorts_clipper.__main__ import build_parser
from shorts_clipper.captions.generator import _ass_header
from shorts_clipper.core.models import ClipWindow
from shorts_clipper.pipeline.profiles import LONG, SHORT, fit_window
from shorts_clipper.rendering.crop import _build_crop_filter


def test_short_finished_length_is_20_to_60s_after_speedup():
    assert SHORT.source_min == pytest.approx(20 * 1.15)
    assert SHORT.source_max == pytest.approx(60 * 1.15)
    assert not SHORT.landscape


def test_long_is_8_to_under_20_min_landscape_without_speedup():
    assert (LONG.source_min, LONG.source_max) == (480, 1140)
    assert LONG.max_seconds < 20 * 60
    assert LONG.pacing == 1.0
    assert LONG.landscape


def test_fit_window_extends_too_short_window():
    w = fit_window(ClipWindow(start=100.0, end=115.0), SHORT)
    assert w.start == 100.0
    assert w.end - w.start == pytest.approx(SHORT.source_min)


def test_fit_window_trims_too_long_window():
    w = fit_window(ClipWindow(start=100.0, end=1500.0), LONG)
    assert w.start == 100.0
    assert w.end - w.start == pytest.approx(LONG.source_max)


def test_fit_window_keeps_window_in_range():
    original = ClipWindow(start=10.0, end=50.0)
    assert fit_window(original, SHORT) == original


def test_landscape_crop_keeps_full_frame():
    vf = _build_crop_filter(1920, 1080, "landscape")
    assert "crop=" not in vf
    assert "scale=1920:1080" in vf


def test_landscape_captions_use_16x9_canvas():
    portrait = _ass_header("default")
    landscape = _ass_header("default", landscape=True)
    assert "PlayResX: 1080" in portrait and "PlayResY: 1920" in portrait
    assert "PlayResX: 1920" in landscape and "PlayResY: 1080" in landscape
    style = next(line for line in landscape.splitlines() if line.startswith("Style:"))
    fields = style.split(",")
    assert fields[2] == "46"  # 58 * 0.8
    assert fields[21] == "60"


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["clip", "u"], (None, 0)),
        (["clip", "u", "-c", "3"], (3, 0)),
        (["clip", "u", "--long", "2", "--short", "1"], (1, 2)),
        (["clip", "u", "--long", "2"], (None, 2)),
    ],
)
def test_cli_long_and_short_flags(argv, expected):
    args = build_parser().parse_args(argv)
    assert (args.count, args.long) == expected
