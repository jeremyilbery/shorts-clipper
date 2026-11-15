"""Long-form metadata: chapter formatting, tag limits, and the kind="long" provider path."""

import json
from types import SimpleNamespace

from shorts_clipper.core.models import TranscriptSegment
from shorts_clipper.providers.gemini import GeminiProvider, format_chapters, limit_tags


def test_chapters_start_at_zero_and_are_sorted():
    out = format_chapters(
        [
            {"start_seconds": 300, "title": "Third"},
            {"start_seconds": 4, "title": "Intro"},
            {"start_seconds": 120, "title": "Second"},
        ],
        duration=600,
    )
    assert out.splitlines() == ["0:00 Intro", "2:00 Second", "5:00 Third"]


def test_chapters_drop_too_close_or_out_of_range_entries():
    out = format_chapters(
        [
            {"start_seconds": 0, "title": "A"},
            {"start_seconds": 5, "title": "Too close"},
            {"start_seconds": 60, "title": "B"},
            {"start_seconds": 200, "title": "C"},
            {"start_seconds": 9999, "title": "Past the end"},
            {"title": "No start"},
            "junk",
        ],
        duration=600,
    )
    assert out.splitlines() == ["0:00 A", "1:00 B", "3:20 C"]


def test_chapters_need_at_least_three():
    assert format_chapters([{"start_seconds": 0, "title": "A"}, {"start_seconds": 60, "title": "B"}], 600) == ""


def test_hour_long_timestamps():
    out = format_chapters(
        [{"start_seconds": s, "title": f"c{s}"} for s in (0, 1800, 3700)], duration=4000
    )
    assert out.splitlines()[-1] == "1:01:40 c3700"


def test_limit_tags_dedupes_strips_shorts_and_caps_length():
    tags = limit_tags(["Joe Rogan", "#JRE", "joe rogan", "shorts", "x" * 300, "y" * 300])
    assert tags[:2] == ["Joe Rogan", "JRE"]
    assert "shorts" not in [t.lower() for t in tags]
    assert sum(len(t) for t in tags) <= 450


def test_long_kind_uses_long_form_prompt_and_appends_chapters():
    provider = GeminiProvider.__new__(GeminiProvider)
    prompts = []

    def fake_generate(prompt, **_):
        prompts.append(prompt)
        return SimpleNamespace(
            text=json.dumps(
                {
                    "selected_title": "Joe Rogan on Park Rangers #shorts",
                    "description": "A long description.",
                    "chapters": [
                        {"start_seconds": 0, "title": "Opening"},
                        {"start_seconds": 120, "title": "Middle"},
                        {"start_seconds": 400, "title": "Ending"},
                    ],
                    "tags": ["Joe Rogan", "shorts", "podcast"],
                }
            )
        )

    provider.generate_content = fake_generate
    segments = [TranscriptSegment(start=0.0, end=600.0, text="talk")]
    meta = provider.generate_clip_metadata(segments, source_title="Ep", kind="long")

    assert "long-form" in prompts[0] and "NOT Shorts" in prompts[0]
    assert meta["title"] == "Joe Rogan on Park Rangers"
    assert meta["description"].endswith("Chapters:\n0:00 Opening\n2:00 Middle\n6:40 Ending")
    assert meta["tags"] == ["Joe Rogan", "podcast"]
