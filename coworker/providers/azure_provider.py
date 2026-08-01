"""Azure AI Foundry — the Responses API when the deployment serves it, Chat Completions
otherwise.

A Foundry resource's `/openai/v1` surface answers BOTH wires, but not for every deployment:
OpenAI models (the gpt-5.x family) serve `/responses`, which is the only way to get reasoning
AND function tools in the same call — Chat Completions rejects that combination outright
("Function tools with reasoning_effort are not supported … use /v1/responses"), at any effort
value including `none` (probed against a live gpt-5.6-terra deployment 2026-07-28). Deployments
of other vendors' models (Grok, DeepSeek, Llama…) have no `/responses` route at all.

The deployment NAME is chosen by the user, so it can't tell us which of the two a deployment
is. Rather than ask the user to classify their own deployments, this client tries Responses
first and falls back to Chat Completions the one time the endpoint says that route isn't
there — then remembers, so the probe costs one request per process, not one per turn.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import ModelCapabilities, ProviderClient
from .capabilities import capabilities_for
from .openai_provider import OpenAIProvider
from .openai_responses import OpenAIResponsesProvider

# What "this endpoint has no /responses route" looks like coming back through the SDK. A
# wrong key or a bad request must NOT trigger the fallback — those are real errors the user
# needs to see, so the match stays narrow.
_NO_RESPONSES_MARKERS = (
    "unknown path",
    "no route",
    "not found",
    "resource not found",
    "unsupported api",
    "operationnotsupported",
    "does not support",
)


def _is_missing_responses_api(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    msg = str(exc).lower()
    if status == 404:
        return True
    return any(marker in msg for marker in _NO_RESPONSES_MARKERS) and "responses" in msg


class AzureFoundryProvider(ProviderClient):
    """Responses-first client for one Foundry resource, with a sticky Chat Completions fallback."""

    # True even though the fallback wire cannot take it: the Chat Completions provider
    # drops the parameter itself when a server rejects it (`_param_fix_retry`).
    accepts_reasoning_effort = True

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self._responses = OpenAIResponsesProvider(api_key=api_key, base_url=base_url)
        self._chat = OpenAIProvider(api_key=api_key, base_url=base_url)
        # Flips once, on the first deployment that has no /responses route. Per client, and
        # the router caches one client per provider — a resource serving both wires from
        # different deployments would pin the whole provider to Chat Completions, which is
        # the safe direction (it works everywhere; it just can't carry reasoning + tools).
        self._chat_only = False

    def capabilities(self, model: str) -> ModelCapabilities:
        return capabilities_for(model)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        if self._chat_only:
            return self._chat.complete(
                model=model, messages=messages, tools=tools, **settings
            )
        try:
            return self._responses.complete(
                model=model, messages=messages, tools=tools, **settings
            )
        except Exception as exc:
            if not _is_missing_responses_api(exc):
                raise
            self._chat_only = True
            return self._chat.complete(
                model=model, messages=messages, tools=tools, **settings
            )

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        if self._chat_only:
            yield from self._chat.stream(
                model=model, messages=messages, tools=tools, **settings
            )
            return
        # The request happens on the first pull, so that is where a missing route surfaces.
        chunks = self._responses.stream(
            model=model, messages=messages, tools=tools, **settings
        )
        try:
            first = next(chunks)
        except StopIteration:
            return
        except Exception as exc:
            if not _is_missing_responses_api(exc):
                raise
            self._chat_only = True
            yield from self._chat.stream(
                model=model, messages=messages, tools=tools, **settings
            )
            return
        yield first
        yield from chunks
