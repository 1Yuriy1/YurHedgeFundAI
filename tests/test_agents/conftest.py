"""Agent-test plumbing: a stub Anthropic-SDK-shaped LLM plus agent factories.

Everything here is offline — the stub never talks to a network, and connector
HTTP is mocked with respx inside individual tests.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from agents.base import LLMMessageParam, LLMTextBlock
from agents.research.fundamentals import FundamentalsAgent
from agents.research.macro_rates import MacroRatesAgent
from agents.research.sec_filings import SecFilingsAgent
from core.config import Settings
from data.connectors.edgar import EdgarConnector
from data.connectors.fmp import FmpConnector
from data.connectors.fred import FredConnector
from fakeredis import FakeRedis

from tests.test_connectors.conftest import FakeClock, make_settings

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=False)


class StubTextBlock:
    """Minimal stand-in for an anthropic text content block."""

    def __init__(self, text: str) -> None:
        self.text = text


class StubResponse:
    """Minimal stand-in for an anthropic Message."""

    def __init__(self, text: str) -> None:
        # Typed as the protocol's member type: a mutable protocol attribute
        # must match invariantly, so `list[StubTextBlock]` would not satisfy
        # LLMResponse even though StubTextBlock has `.text`.
        self.content: Sequence[LLMTextBlock] = [StubTextBlock(text)]


class StubMessages:
    """``client.messages`` port: records the request, serves canned text."""

    def __init__(self, stub: "StubLLM") -> None:
        self._stub = stub

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[LLMMessageParam],
    ) -> StubResponse:
        if not self._stub.responses:
            raise AssertionError("StubLLM ran out of canned responses")
        self._stub.calls.append(
            {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "user": messages[-1]["content"],
            }
        )
        return StubResponse(self._stub.responses.pop(0))


class StubLLM:
    """Anthropic-SDK-shaped stub: pops one canned response text per call."""

    def __init__(self, *responses: str) -> None:
        self.responses: list[str] = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages: StubMessages = StubMessages(self)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def canned_response(fixture_name: str) -> str:
    """A canned LLM reply stored verbatim under tests/fixtures/."""
    return (FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")


def last_user_payload(stub: StubLLM) -> str:
    """The user content of the most recent LLM call."""
    user = stub.calls[-1]["user"]
    assert isinstance(user, str)
    return user


def make_macro_agent(
    fake_clock: FakeClock,
    fake_redis: FakeRedis | None,
    stub: StubLLM,
    settings: Settings | None = None,
) -> MacroRatesAgent:
    """MacroRatesAgent over a live connector object pointed at respx."""
    resolved = settings or make_settings()
    fred = FredConnector(
        resolved,
        redis_client=fake_redis,
        clock=fake_clock.now,
        sleeper=fake_clock.sleep,
    )
    return MacroRatesAgent(llm=stub, settings=resolved, fred=fred)


def make_sec_agent(
    fake_clock: FakeClock,
    stub: StubLLM,
    settings: Settings | None = None,
) -> SecFilingsAgent:
    """SecFilingsAgent over a live connector object pointed at respx."""
    resolved = settings or make_settings()
    edgar = EdgarConnector(
        resolved,
        redis_client=None,
        clock=fake_clock.now,
        sleeper=fake_clock.sleep,
    )
    return SecFilingsAgent(llm=stub, settings=resolved, edgar=edgar)


def make_fmp_agent(
    fake_clock: FakeClock,
    fake_redis: FakeRedis | None,
    stub: StubLLM,
    settings: Settings | None = None,
) -> FundamentalsAgent:
    """FundamentalsAgent with FMP enabled (pass fmp_api_key=None to disable)."""
    resolved = settings or make_settings(fmp_api_key="test-fmp-key")
    fmp: FmpConnector | None = None
    if resolved.fmp_api_key:
        fmp = FmpConnector(
            resolved,
            redis_client=fake_redis,
            clock=fake_clock.now,
            sleeper=fake_clock.sleep,
        )
    return FundamentalsAgent(llm=stub, settings=resolved, fmp=fmp)


def signals_from_fixture(fixture_name: str) -> list[dict[str, Any]]:
    """Parse a canned-signal fixture (used to build invalid variants)."""
    decoded = json.loads(canned_response(fixture_name))
    assert isinstance(decoded, list)
    return decoded
