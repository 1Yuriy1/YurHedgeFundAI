"""Anthropic SDK adapter — the one place the SDK's concrete types enter.

``LLMClient`` (agents/base.py) is a structural protocol so agents stay
offline-testable with stubs; the real SDK client does not satisfy it
directly (its ``Message`` content blocks are a wider union than
``LLMResponse``'s text-block contract). This adapter pins the narrow
protocol shape over the SDK and is imported only by the CLI composition
root — never by agents, pipelines, or tests.
"""

from collections.abc import Callable
from typing import Any

import anthropic

from agents.base import LLMMessageParam


class _AnthropicCreatePort:
    """``messages.create`` restricted to the protocol's kwargs, verbatim.

    Returns the SDK ``Message``; at the adapter boundary the response is
    typed ``Any`` on purpose — the SDK's content-block union is wider than
    ``LLMResponse``, and ``extract_response_text`` already narrows it by
    keeping only string ``text`` blocks.
    """

    def __init__(self, create: Callable[..., Any]) -> None:
        self._create = create

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[LLMMessageParam],
    ) -> Any:
        return self._create(model=model, max_tokens=max_tokens, system=system, messages=messages)


class AnthropicLLM:
    """``LLMClient`` backed by the real Anthropic SDK client."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client or anthropic.Anthropic()

    @property
    def messages(self) -> _AnthropicCreatePort:
        return _AnthropicCreatePort(self._client.messages.create)
