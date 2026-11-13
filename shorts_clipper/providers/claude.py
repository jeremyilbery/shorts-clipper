"""Claude AI provider.

Drop-in alternative to GeminiProvider. It reuses every prompt and parsing
routine from GeminiProvider and only swaps the transport: requests go to the
Anthropic Messages API instead of Gemini.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from shorts_clipper.core.exceptions import ProviderError
from shorts_clipper.providers.gemini import GeminiProvider

log = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_CLAUDE_EFFORT = "medium"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_JSON_INSTRUCTION = (
    "Respond with a single valid JSON value and nothing else: "
    "no markdown code fences, no commentary before or after it."
)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ClaudeResponse:
    """Minimal response object exposing `.text`, like Gemini's response."""

    text: str


def _config_value(config: Any, name: str) -> Any:
    if config is None:
        return None
    if isinstance(config, dict):
        return config.get(name)
    return getattr(config, name, None)


def _extract_json(text: str) -> str:
    """Strip code fences / surrounding prose so callers can json.loads() directly."""
    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    if text[:1] in "{[":
        return text
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return text
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else text


class ClaudeProvider(GeminiProvider):
    """Uses Claude for clip selection, relevance scoring and metadata."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        fallback_window: tuple[float, float] = (60.0, 95.0),
        fallback_layout: str = "crop_center",
    ) -> None:
        import anthropic

        self._model = model or os.environ.get("SHORTS_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL
        self._effort = effort or os.environ.get("SHORTS_CLAUDE_EFFORT") or DEFAULT_CLAUDE_EFFORT
        self._fallback_window = fallback_window
        self._fallback_layout = fallback_layout
        # With api_key=None the SDK resolves credentials itself (ANTHROPIC_API_KEY, etc.).
        # The SDK retries 429 / 5xx / connection errors with backoff on its own.
        self._client = anthropic.Anthropic(api_key=api_key or None, max_retries=4)

    def generate_content(
        self, contents: Any, max_retries: int = 5, **kwargs: Any
    ) -> ClaudeResponse:
        """Send a prompt to Claude and return an object with a `.text` attribute.

        Accepts the Gemini-style `config=` argument used around the codebase:
        `system_instruction`, `response_mime_type` and `max_output_tokens` are
        honoured; sampling settings such as `temperature` are ignored because
        current Claude models do not accept them.
        """
        import anthropic

        config = kwargs.get("config")
        wants_json = _config_value(config, "response_mime_type") == "application/json"
        system_parts = []
        system_instruction = _config_value(config, "system_instruction")
        if system_instruction:
            system_parts.append(str(system_instruction))
        if wants_json:
            system_parts.append(_JSON_INSTRUCTION)

        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": int(_config_value(config, "max_output_tokens") or 16000),
            "messages": [{"role": "user", "content": str(contents)}],
            "output_config": {"effort": self._effort},
            # If the model declines a request, let the API retry on a fallback model.
            "betas": [_FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system_parts:
            request["system"] = "\n\n".join(system_parts)

        try:
            message = self._client.beta.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            log.error("ANTHROPIC API KEY INVALID OR MISSING: %s", exc)
            raise ProviderError(f"Anthropic API key is invalid or missing: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError(f"Anthropic API key lacks permission: {exc}") from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"Claude rejected the request: {exc}") from exc
        except anthropic.RateLimitError as exc:
            log.error("Claude rate limit still exceeded after retries: %s", exc)
            raise
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc)
            raise
        except anthropic.APIConnectionError as exc:
            log.error("Could not reach the Claude API: %s", exc)
            raise

        if message.stop_reason == "refusal":
            raise ProviderError("Claude declined this request.")
        if message.stop_reason == "max_tokens":
            log.warning("Claude response was cut off at max_tokens; output may be incomplete.")

        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise ProviderError("Claude returned an empty response.")
        return ClaudeResponse(text=_extract_json(text) if wants_json else text)
