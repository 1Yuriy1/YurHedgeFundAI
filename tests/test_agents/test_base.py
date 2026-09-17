"""BaseAgent contract tests: prompts, tiering, validation, retry-once, stamping."""

import json
import logging
from typing import Any, ClassVar

import pytest
from agents.base import BaseAgent, LLMOutputError, PromptNotFoundError, load_prompt
from core.config import Settings
from core.schemas import Signal

from tests.test_connectors.conftest import make_settings

from .conftest import StubLLM, canned_response, signals_from_fixture

VALID = canned_response("signals_macro.json")


class DummyAgent(BaseAgent):
    """Minimal concrete agent for exercising the BaseAgent machinery."""

    name: ClassVar[str] = "dummy"
    prompt_filename: ClassVar[str] = "macro_rates_v1.md"

    def collect(self, tickers: list[str]) -> dict[str, Any]:
        return {"tickers": tickers}

    def analyze(self, tickers: list[str], data: dict[str, Any]) -> list[Signal]:
        return self.request_signals(data.get("payload", "[]"))


def make_agent(stub: StubLLM, settings: Settings | None = None) -> DummyAgent:
    resolved = settings or make_settings(agent_tiers={"dummy": 5})
    return DummyAgent(llm=stub, settings=resolved)


def test_versioned_prompt_loads_and_embeds_evidence_rule() -> None:
    text = load_prompt("macro_rates_v1.md")

    # The Signal JSON schema is embedded (field-level, not by word).
    for field in ("strength", "confidence", "horizon", "red_flag", "thesis", "evidence", "risks"):
        assert f'"{field}"' in text, f"schema field {field!r} missing from prompt"
    assert "URL-backed" in text  # the evidence rule is embedded
    assert "exactly" in text  # cite figures exactly as returned


def test_missing_prompt_fails_at_construction() -> None:
    with pytest.raises(PromptNotFoundError):
        load_prompt("does_not_exist_v99.md")

    class NoPromptAgent(DummyAgent):
        prompt_filename: ClassVar[str] = "does_not_exist_v99.md"

    with pytest.raises(PromptNotFoundError):
        NoPromptAgent(llm=StubLLM(VALID), settings=make_settings(agent_tiers={"dummy": 5}))


def test_tier_and_model_come_from_config() -> None:
    settings = make_settings(agent_tiers={"dummy": 5}, anthropic_model_analyst="test-analyst-model")
    agent = make_agent(StubLLM(VALID), settings)

    assert agent.tier == 5
    assert agent.model == "test-analyst-model"


def test_missing_tier_fails_loudly() -> None:
    settings = make_settings(agent_tiers={})

    with pytest.raises(ValueError, match="dummy"):
        DummyAgent(llm=StubLLM(VALID), settings=settings)


def test_valid_canned_response_is_validated_and_stamped() -> None:
    stub = StubLLM(VALID)
    agent = make_agent(stub)

    signals = agent.request_signals("raw data payload")

    assert len(signals) == 1
    signal = signals[0]
    assert signal.agent == "dummy"  # stamped, not trusted from the model
    assert signal.tier == 5
    assert signal.timestamp.tzinfo is not None
    assert len(signal.evidence) >= 1


def test_spoofed_agent_and_tier_fields_are_overridden() -> None:
    """A model claiming another agent's identity still gets platform stamps."""
    spoofed = json.dumps(
        [{**signals_from_fixture("signals_macro.json")[0], "agent": "impostor", "tier": 9}]
    )
    agent = make_agent(StubLLM(spoofed))

    signals = agent.request_signals("payload")

    assert len(signals) == 1
    assert signals[0].agent == "dummy"
    assert signals[0].tier == 5


def test_malformed_output_is_retried_once_then_recovered() -> None:
    stub = StubLLM("not json at all", VALID)
    agent = make_agent(stub)

    signals = agent.request_signals("payload")

    assert len(signals) == 1
    assert stub.call_count == 2


def test_persistently_malformed_output_is_dropped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub = StubLLM("garbage {", "still not json")
    agent = make_agent(stub)

    with caplog.at_level(logging.WARNING, logger="agents.base"):
        signals = agent.request_signals("payload")

    assert signals == []  # never an unvalidated signal
    assert stub.call_count == 2  # exactly one retry
    dropped = [record for record in caplog.records if "signal_dropped" in record.message]
    assert len(dropped) == 1
    assert getattr(dropped[0], "agent", None) == "dummy"
    assert getattr(dropped[0], "attempt", None) == 2


def test_partially_invalid_arrays_never_emit_unvalidated_signals() -> None:
    valid = signals_from_fixture("signals_macro.json")[0]
    missing_evidence = {**valid, "evidence": []}
    stub = StubLLM(
        json.dumps([valid, missing_evidence]),  # item 1 invalid
        json.dumps([valid]),
    )
    agent = make_agent(stub)

    signals = agent.request_signals("payload")

    assert len(signals) == 1
    assert all(signal.evidence for signal in signals)
    assert stub.call_count == 2


def test_both_attempts_invalid_drops_everything(caplog: pytest.LogCaptureFixture) -> None:
    valid = signals_from_fixture("signals_macro.json")[0]
    missing_evidence = {**valid, "evidence": []}
    bad = json.dumps([valid, missing_evidence])
    stub = StubLLM(bad, bad)
    agent = make_agent(stub)

    with caplog.at_level(logging.WARNING, logger="agents.base"):
        signals = agent.request_signals("payload")

    assert signals == []
    assert stub.call_count == 2


def test_code_fenced_json_is_accepted() -> None:
    fenced = f"```json\n{VALID}\n```"
    agent = make_agent(StubLLM(fenced))

    signals = agent.request_signals("payload")

    assert len(signals) == 1


def test_empty_array_is_silence_not_malformation() -> None:
    stub = StubLLM("[]")
    agent = make_agent(stub)

    assert agent.request_signals("payload") == []
    assert stub.call_count == 1  # no retry for legitimate silence


def test_llm_call_receives_config_model_budget_and_prompt() -> None:
    settings = make_settings(
        agent_tiers={"dummy": 5},
        anthropic_model_analyst="test-analyst-model",
        anthropic_max_tokens=1234,
    )
    stub = StubLLM(VALID)
    agent = make_agent(stub, settings)

    agent.request_signals("raw data payload")

    call = stub.calls[0]
    assert call["model"] == "test-analyst-model"
    assert call["max_tokens"] == 1234
    assert "URL-backed" in call["system"]
    assert call["user"] == "raw data payload"


def test_run_wires_collect_into_analyze() -> None:
    stub = StubLLM(VALID)
    agent = make_agent(stub)

    signals = agent.run(["AAPL"])

    assert len(signals) == 1
    assert stub.call_count == 1
    assert agent.collect(["AAPL"]) == {"tickers": ["AAPL"]}


def test_llm_output_error_is_a_value_error() -> None:
    """Callers catching validation failures can catch ValueError."""
    assert issubclass(LLMOutputError, ValueError)
