import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shorts_clipper.core.exceptions import ProviderError
from shorts_clipper.core.settings import Settings
from shorts_clipper.providers import get_provider
from shorts_clipper.providers.claude import ClaudeProvider, _extract_json
from shorts_clipper.providers.gemini import TranscriptSegment


def _message(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )


def _provider(reply, stop_reason="end_turn"):
    with patch("anthropic.Anthropic") as client_cls:
        provider = ClaudeProvider(api_key="test-key")
    create = client_cls.return_value.beta.messages.create
    create.return_value = _message(reply, stop_reason)
    return provider, create


def test_factory_selects_claude():
    with patch("anthropic.Anthropic") as client_cls:
        provider = get_provider(Settings(anthropic_api_key="k", default_provider="anthropic"))
    assert isinstance(provider, ClaudeProvider)
    assert client_cls.call_args.kwargs["api_key"] == "k"


def test_factory_rejects_unknown_provider():
    with pytest.raises(ValueError):
        get_provider(Settings(default_provider="openai"))


def test_generate_content_builds_claude_request():
    provider, create = _provider("hello")
    assert provider.generate_content("hi").text == "hello"
    request = create.call_args.kwargs
    assert request["model"] == "claude-opus-5-5"
    assert request["messages"] == [{"role": "user", "content": "hi"}]
    assert "temperature" not in request
    assert "system" not in request


def test_json_config_strips_fences_and_ignores_temperature():
    provider, create = _provider('```json\n{"best_title": "X"}\n```')
    response = provider.generate_content(
        "p", config={"response_mime_type": "application/json", "temperature": 0.0}
    )
    assert json.loads(response.text) == {"best_title": "X"}
    request = create.call_args.kwargs
    assert "JSON" in request["system"]
    assert "temperature" not in request


def test_extract_json_from_surrounding_prose():
    assert json.loads(_extract_json('Here you go:\n[{"id": "a"}]\nDone.')) == [{"id": "a"}]


def test_refusal_raises_provider_error():
    provider, _ = _provider("", stop_reason="refusal")
    with pytest.raises(ProviderError):
        provider.generate_content("p")


def test_metadata_generation_reuses_shared_parsing():
    reply = json.dumps({"selected_title": "Big Hook", "description": "d", "tags": ["a", "b"]})
    provider, _ = _provider(reply)
    segments = [TranscriptSegment(start=0.0, end=2.0, text="Have you ever wondered why?")]
    result = provider.generate_clip_metadata(segments)
    assert result == {"title": "Big Hook", "description": "d", "tags": ["a", "b"]}
