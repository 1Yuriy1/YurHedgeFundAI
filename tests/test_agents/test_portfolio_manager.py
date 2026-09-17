"""Portfolio Manager agent tests — model from config, retry semantics, offline."""

import pytest
from agents.base import LLMOutputError
from agents.leadership.portfolio_manager import PortfolioManagerAgent
from core.config import Settings

from tests.test_agents.conftest import StubLLM, last_user_payload
from tests.test_connectors.conftest import make_settings

PM_TEXT = "Yield-curve inversion leads; the ladder stays defensive into the open."


def make_pm(stub: StubLLM, settings: Settings | None = None) -> PortfolioManagerAgent:
    return PortfolioManagerAgent(llm=stub, settings=settings or make_settings())


def test_pm_uses_the_opus_class_model_from_config() -> None:
    """Locked decision 2: the PM's model ID is the config value, not a literal."""
    settings = make_settings(anthropic_model_pm="claude-opus-test-id")
    stub = StubLLM(PM_TEXT)

    pm = make_pm(stub, settings)

    assert pm.model == "claude-opus-test-id"
    pm.synthesize("{}")
    assert stub.calls[0]["model"] == "claude-opus-test-id"


def test_pm_prompt_is_loaded_from_prompts_dir() -> None:
    stub = StubLLM(PM_TEXT)
    pm = make_pm(stub)
    assert "Tier 1" in pm.system_prompt
    assert "Tier 7" in pm.system_prompt
    pm.synthesize("{}")
    assert stub.calls[0]["system"] == pm.system_prompt


def test_pm_synth_payload_reaches_the_model() -> None:
    stub = StubLLM(PM_TEXT)

    make_pm(stub).synthesize('{"ranked_ideas": [], "gaps": []}')

    assert last_user_payload(stub) == '{"ranked_ideas": [], "gaps": []}'


def test_pm_retries_once_on_empty_output() -> None:
    stub = StubLLM("", PM_TEXT)

    text = make_pm(stub).synthesize("{}")

    assert text == PM_TEXT
    assert stub.call_count == 2


def test_pm_raises_after_both_attempts_empty() -> None:
    stub = StubLLM("", "")

    with pytest.raises(LLMOutputError, match="no content"):
        make_pm(stub).synthesize("{}")

    assert stub.call_count == 2
