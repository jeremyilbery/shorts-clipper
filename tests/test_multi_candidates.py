"""SemanticCandidateGenerator.generate_candidates returns several non-overlapping windows."""

import json
from types import SimpleNamespace
from unittest import mock

from shorts_clipper.core.models import TranscriptSegment
from shorts_clipper.highlight_detection.scoring import SemanticCandidateGenerator


def _segments():
    # 10-second segments covering 0–300s
    return [TranscriptSegment(start=float(t), end=float(t + 10), text=f"s{t}") for t in range(0, 300, 10)]


def _generator(highlights):
    gen = SemanticCandidateGenerator.__new__(SemanticCandidateGenerator)
    gen.settings = None
    provider = mock.Mock()
    provider.generate_content.return_value = SimpleNamespace(
        text=json.dumps({"highlights": highlights})
    )
    return gen, provider


def _h(start, end, score):
    return {
        "start_time": start,
        "end_time": end,
        "score": score,
        "signals_triggered": ["tension"],
        "why_it_won": "test",
    }


def test_returns_requested_count_best_first_without_overlap():
    gen, provider = _generator(
        [_h(0, 40, 70), _h(100, 140, 95), _h(120, 160, 90), _h(200, 240, 80)]
    )
    with mock.patch("shorts_clipper.providers.get_provider", return_value=provider):
        results = gen.generate_candidates(_segments(), count=3)

    assert [r[0] for r in results] == [95.0, 80.0, 70.0]  # 120–160 overlaps 100–140, so skipped
    starts = [r[1][0].start for r in results]
    assert starts == [100.0, 200.0, 0.0]


def test_returns_fewer_when_not_enough_distinct_highlights():
    gen, provider = _generator([_h(100, 140, 95), _h(110, 150, 90)])
    with mock.patch("shorts_clipper.providers.get_provider", return_value=provider):
        results = gen.generate_candidates(_segments(), count=3)

    assert len(results) == 1
    assert results[0][0] == 95.0


def test_generate_candidate_still_returns_single_best():
    gen, provider = _generator([_h(0, 40, 70), _h(100, 140, 95)])
    with mock.patch("shorts_clipper.providers.get_provider", return_value=provider):
        score, window, _ = gen.generate_candidate(_segments())

    assert score == 95.0
    assert window[0].start == 100.0
