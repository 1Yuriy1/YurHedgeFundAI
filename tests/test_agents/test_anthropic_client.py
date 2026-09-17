"""Anthropic SDK adapter tests — kwargs pass-through and protocol shape."""

from typing import Any

from agents.anthropic_client import AnthropicLLM
from agents.base import LLMClient


class FakeSDKMessage:
    text = "live synthesis"


class FakeSDKContent:
    content = [FakeSDKMessage()]


class FakeSDKMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeSDKContent:
        self.calls.append(kwargs)
        return FakeSDKContent()


class FakeSDKClient:
    def __init__(self) -> None:
        self.messages = FakeSDKMessages()


def make_adapter() -> tuple[AnthropicLLM, FakeSDKMessages]:
    client = FakeSDKClient()
    return AnthropicLLM(client), client.messages  # type: ignore[arg-type]


def test_adapter_is_an_llmclient() -> None:
    """Structural check: the adapter satisfies the agents' protocol."""
    adapter, _ = make_adapter()

    llm: LLMClient = adapter

    assert llm is not None


def test_adapter_forwards_the_protocol_kwargs() -> None:
    adapter, messages = make_adapter()

    response = adapter.messages.create(  # type: ignore[union-attr]
        model="claude-opus-test-id",
        max_tokens=1024,
        system="system prompt",
        messages=[{"role": "user", "content": "payload"}],
    )

    assert messages.calls == [
        {
            "model": "claude-opus-test-id",
            "max_tokens": 1024,
            "system": "system prompt",
            "messages": [{"role": "user", "content": "payload"}],
        }
    ]
    assert response.content[0].text == "live synthesis"
