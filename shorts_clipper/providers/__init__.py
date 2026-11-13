"""AI provider interfaces and adapters."""

from __future__ import annotations

from typing import Any

_CLAUDE_NAMES = {"anthropic", "claude"}


def is_claude(settings: Any) -> bool:
    return (settings.default_provider or "").strip().lower() in _CLAUDE_NAMES


def get_provider(settings: Any = None, **kwargs: Any):
    """Return the AI provider selected by SHORTS_PROVIDER (Claude by default)."""
    if settings is None:
        from shorts_clipper.core.settings import Settings

        settings = Settings.from_env()

    name = (settings.default_provider or "").strip().lower()
    if name in _CLAUDE_NAMES:
        from shorts_clipper.providers.claude import ClaudeProvider

        return ClaudeProvider(api_key=settings.anthropic_api_key, **kwargs)
    if name == "gemini":
        from shorts_clipper.providers.gemini import GeminiProvider

        return GeminiProvider(api_key=settings.gemini_api_key, **kwargs)
    raise ValueError(f"Unsupported SHORTS_PROVIDER {name!r}; use 'anthropic' or 'gemini'.")
