"""Deterministic highlight scoring heuristics."""

from __future__ import annotations

import re

from shorts_clipper.core.models import HighlightScore, TranscriptSegment

HOOK_PATTERNS = (
    "you won't believe",
    "you will not believe",
    "secret",
    "mistake",
    "watch this",
    "here's why",
    "this is why",
    "changed everything",
    "the truth",
)
EMOTION_WORDS = {
    "amazing",
    "insane",
    "crazy",
    "shocking",
    "unbelievable",
    "love",
    "hate",
    "angry",
    "terrified",
    "secret",
    "best",
    "worst",
    "changed",
    "everything",
}
VIRAL_WORDS = {
    "secret",
    "hack",
    "trick",
    "mistake",
    "truth",
    "viral",
    "million",
    "money",
}


class RuleBasedHighlightScorer:
    def score_segment(self, segment: TranscriptSegment) -> HighlightScore:
        text = segment.text.strip()
        lower = text.lower()
        words = re.findall(r"[a-zA-Z']+", lower)
        word_count = len(words)
        duration = max(segment.duration, 0.001)
        words_per_second = word_count / duration

        hook = 0.0
        if any(pattern in lower for pattern in HOOK_PATTERNS):
            hook += 2.0
        if text.endswith("?") or lower.startswith(("why", "how", "what", "here")):
            hook += 0.75

        emotion_hits = sum(1 for word in words if word in EMOTION_WORDS)
        virality_hits = sum(1 for word in words if word in VIRAL_WORDS)

        emotion = min(2.0, emotion_hits * 0.35 + text.count("!") * 0.25)
        virality = min(2.0, virality_hits * 0.4)
        retention = (
            1.0 if 15 <= segment.duration <= 60 else max(0.0, 1.0 - abs(segment.duration - 35) / 35)
        )
        silence = 1.0 if words_per_second >= 1.6 else words_per_second / 1.6
        topic = min(1.0, max(0.0, word_count / 20))
        speaker_emphasis = min(1.0, text.count("!") * 0.3 + text.count("?") * 0.2)
        caption_density = (
            min(1.0, len(segment.words) / max(1, word_count)) if segment.words else 0.0
        )

        return HighlightScore(
            hook=hook,
            emotion=emotion,
            silence=silence,
            retention=retention,
            virality=virality,
            topic=topic,
            speaker_emphasis=speaker_emphasis,
            caption_density=caption_density,
            metadata={
                "word_count": word_count,
                "words_per_second": round(words_per_second, 3),
                "emotion_hits": emotion_hits,
                "virality_hits": virality_hits,
            },
        )


class AttentionScore:
    def __init__(self, score: float, reasoning: str, window: list[TranscriptSegment]):
        self.score = score
        self.reasoning = reasoning
        self.window = window


class SemanticCandidateGenerator:
    """Analyzes transcripts using Gemini Flash to generate candidate semantic windows."""

    def __init__(self):
        from shorts_clipper.core.settings import Settings

        self.settings = Settings.from_env()

    def generate_candidate(
        self,
        segments: list[TranscriptSegment],
        *,
        min_seconds: float = 20.0,
        max_seconds: float = 60.0,
    ) -> tuple[float, list[TranscriptSegment], str]:
        """Identifies the best semantic window and returns its generation score, the window, and reasoning."""
        return self.generate_candidates(
            segments, count=1, min_seconds=min_seconds, max_seconds=max_seconds
        )[0]

    @staticmethod
    def _map_window(
        segments: list[TranscriptSegment], start_t: float, end_t: float
    ) -> list[TranscriptSegment]:
        """Map target bounds to the closest raw segments.

        Post-processing sentence snapping is handled exactly once by EditorialFinisher in runner.py.
        """
        start_idx = 0
        end_idx = len(segments) - 1

        for i, s in enumerate(segments):
            if s.start >= start_t:
                start_idx = i
                break

        for i in range(start_idx, len(segments)):
            if segments[i].end >= end_t:
                end_idx = i
                break

        return segments[start_idx : end_idx + 1]

    def generate_candidates(
        self,
        segments: list[TranscriptSegment],
        count: int = 1,
        *,
        min_seconds: float = 20.0,
        max_seconds: float = 60.0,
    ) -> list[tuple[float, list[TranscriptSegment], str]]:
        """Identify up to ``count`` non-overlapping semantic windows, best first.

        Each entry is (generation score, window, reasoning). Always returns at least one
        entry; a failed or empty result is reported as a single zero-score entry.
        """
        if not segments:
            return [(0.0, [], "Empty transcript")]

        import json
        import logging

        log = logging.getLogger(__name__)

        count = max(1, count)
        requested = max(3, count)

        full_text_parts = []
        for s in segments:
            full_text_parts.append(f"[{s.start:.1f} - {s.end:.1f}] {s.text}")
        full_text = "\n".join(full_text_parts)

        prompt = f"""
You are an Attention Prediction Engine.
Analyze the following transcript and identify the TOP {requested} BEST highlights.
Each highlight MUST be between {min_seconds:.0f}s and {max_seconds:.0f}s long (end_time - start_time).
A highlight must be a complete, self-contained segment that makes sense on its own.
Make each highlight as long as the moment needs within that range: start where the topic starts and end where it naturally wraps up. Don't cut a story off early to stay short, and don't pad it with unrelated talk to get longer.
Highlights must NOT overlap in time.
Evaluate based on true semantics: conflict, surprise, stakes, emotion, transformation, curiosity, tension, and resolution.
Do NOT rely on simple keywords. Look for story progression and emotional peaks.

Transcript:
{full_text}

Return ONLY valid JSON in this format:
{{
    "highlights": [
        {{
            "start_time": float,
            "end_time": float,
            "score": float (0-100),
            "signals_triggered": ["list", "of", "signals"],
            "why_it_won": "detailed explanation of emotional arc and tension"
        }}
    ]
}}
"""

        try:
            from shorts_clipper.providers import get_provider

            provider = get_provider(self.settings)

            response = provider.generate_content(
                contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0.0},
            )

            res = json.loads(response.text)
            highlights = res.get("highlights", [])
            if not highlights:
                return [(0.0, [], "No highlights returned")]

            # Best first; greedily keep highlights that don't overlap an already-chosen one.
            highlights.sort(key=lambda x: x.get("score", 0), reverse=True)
            results: list[tuple[float, list[TranscriptSegment], str]] = []
            taken: list[tuple[float, float]] = []

            for h in highlights:
                if len(results) >= count:
                    break
                start_t = float(h["start_time"])
                end_t = float(h["end_time"])
                if any(start_t < t_end and end_t > t_start for t_start, t_end in taken):
                    continue

                window = self._map_window(segments, start_t, end_t)
                if not window:
                    continue

                signals = ", ".join(h.get("signals_triggered", []))
                reasoning = f"Signals: {signals}. Why it won: {h.get('why_it_won', '')}."
                reasoning += (
                    f" | Raw Semantic Window Duration: {window[-1].end - window[0].start:.1f}s"
                )
                results.append((float(h.get("score", 0)), window, reasoning))
                taken.append((start_t, end_t))

            if not results:
                return [(0.0, [], "Failed to map window")]

            if len(results) < count:
                log.warning(
                    "Requested %d highlights but only %d non-overlapping ones were found.",
                    count,
                    len(results),
                )
            return results

        except Exception as e:
            log.warning("Semantic scoring failed: %s", e)
            # True fallback (if offline)
            return [(0.0, segments[:10], f"Failed semantic scoring: {e}")]
