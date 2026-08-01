"""Azure AI Foundry client: Responses first, Chat Completions when a deployment has no
/responses route. SDK-free — both wires are faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coworker.providers.azure_provider import AzureFoundryProvider, _is_missing_responses_api
from coworker.providers.base import StreamChunk


class _NotFound(Exception):
    status_code = 404


def _provider(responses_raises: Exception | None = None) -> AzureFoundryProvider:
    """A provider whose two wires are stubs recording their calls."""
    p = AzureFoundryProvider(api_key="k", base_url="https://res/openai/v1")

    calls: dict[str, list] = {"responses": [], "chat": []}

    def responses_complete(**kw):
        calls["responses"].append(kw)
        if responses_raises is not None:
            raise responses_raises
        return SimpleNamespace(text="via-responses")

    def responses_stream(**kw):
        calls["responses"].append(kw)
        if responses_raises is not None:
            raise responses_raises
        yield StreamChunk(text_delta="via-responses")

    def chat_complete(**kw):
        calls["chat"].append(kw)
        return SimpleNamespace(text="via-chat")

    def chat_stream(**kw):
        calls["chat"].append(kw)
        yield StreamChunk(text_delta="via-chat")

    p._responses = SimpleNamespace(complete=responses_complete, stream=responses_stream)
    p._chat = SimpleNamespace(complete=chat_complete, stream=chat_stream)
    p.calls = calls  # type: ignore[attr-defined]
    return p


def test_prefers_the_responses_wire():
    p = _provider()
    assert p.complete(model="gpt-5.6-terra", messages=[]).text == "via-responses"
    assert len(p.calls["responses"]) == 1 and not p.calls["chat"]


def test_falls_back_once_then_stays_on_chat_completions():
    """A deployment with no /responses route (Grok, DeepSeek…) must keep working, and must
    not re-probe the missing route on every turn."""
    p = _provider(responses_raises=_NotFound("404 page not found"))

    assert p.complete(model="grok-deploy", messages=[]).text == "via-chat"
    assert p.complete(model="grok-deploy", messages=[]).text == "via-chat"
    assert len(p.calls["responses"]) == 1  # probed once
    assert len(p.calls["chat"]) == 2


def test_streaming_falls_back_on_the_first_pull():
    p = _provider(responses_raises=_NotFound("404 page not found"))
    out = list(p.stream(model="grok-deploy", messages=[]))
    assert [c.text_delta for c in out] == ["via-chat"]


def test_real_errors_are_never_swallowed():
    """A bad key or a rejected request must surface, not silently downgrade the wire."""
    p = _provider(responses_raises=RuntimeError("401 Invalid API key"))
    with pytest.raises(RuntimeError, match="401"):
        p.complete(model="gpt-5.6-terra", messages=[])
    assert not p.calls["chat"]


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_NotFound("not found"), True),
        (RuntimeError("Unknown path /openai/v1/responses"), True),
        (RuntimeError("This model does not support the responses API"), True),
        (RuntimeError("401 Invalid API key"), False),
        (RuntimeError("400 unsupported parameter: temperature"), False),
        # "not found" about something OTHER than the route is a real error
        (RuntimeError("deployment not found"), False),
    ],
)
def test_fallback_signal_is_narrow(exc, expected):
    assert _is_missing_responses_api(exc) is expected


def test_registry_builds_the_azure_wire_picker():
    from coworker.providers.registry import build_provider_client

    client = build_provider_client(
        "azure",
        {"endpoint": "https://res.services.ai.azure.com", "api_key": "az"},
        None,
    )
    assert isinstance(client, AzureFoundryProvider)
    assert client._responses._base_url == "https://res.services.ai.azure.com/openai/v1"
    assert client._chat._base_url == "https://res.services.ai.azure.com/openai/v1"
