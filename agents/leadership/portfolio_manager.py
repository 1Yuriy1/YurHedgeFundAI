"""Portfolio Manager — the leadership agent that synthesizes the daily brief.

The PM is deliberately NOT a BaseAgent: analysts collect data and emit
schema-validated Signals (a tiered vote in the ladder); the PM consumes
already-scored results and gaps and returns prose. It holds no tier, casts
no vote, and its only output is the markdown synthesis embedded in the brief.

Locked decision 2 applies unchanged: the model ID comes from config
(``anthropic_model_pm`` — the Opus-class role), never hardcoded. The system
prompt (``prompts/portfolio_manager_v1.md``) embeds the Master Plan's ladder
order and the evidence/no-invention rules; the payload it receives carries
only mechanically computed scores, leader names, and evidence URLs, so the
PM never has to trust its own memory of the data.
"""

import logging
from typing import Final

from core.config import Settings

from agents.base import (
    LLMClient,
    LLMOutputError,
    extract_response_text,
    load_prompt,
)

logger = logging.getLogger(__name__)

PM_RETRIES: Final[int] = 2


class PortfolioManagerAgent:
    """The leadership agent: ranked scores + gaps in, brief synthesis out.

    Deliberately not a BaseAgent: the analyst base is a collect → analyze
    signal contract (tier-stamped, retried into silence). The PM's contract
    is one Opus call whose output is prose — the only Opus call in the
    platform (locked decision 2).
    """

    name: str = "portfolio_manager"
    prompt_filename: str = "portfolio_manager_v1.md"

    def __init__(self, *, llm: LLMClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings
        # Locked decision 2: model IDs live in config, never hardcoded.
        self.model: str = settings.anthropic_model_pm
        # A missing prompt fails at construction, not mid-pipeline.
        self.system_prompt: str = load_prompt(self.prompt_filename)

    def synthesize(self, user_payload: str) -> str:
        """One Opus call over the scored-run payload; empty output retries once.

        Returns the markdown synthesis body. Unlike the analysts (whose
        dropped signal is merely a missing vote), the PM's output IS the
        deliverable — two empty responses raise rather than degrade the
        brief to silence.
        """
        for attempt in range(1, PM_RETRIES + 1):
            raw = self._call_llm(user_payload)
            if raw:
                return raw
            logger.warning(
                "pm_synthesis_empty",
                extra={"agent": self.name, "attempt": attempt, "of": PM_RETRIES},
            )
        raise LLMOutputError(f"PM synthesis returned no content after {PM_RETRIES} attempts")

    def _call_llm(self, user_payload: str) -> str:
        """The single Opus-class call the whole platform reserves for the PM."""
        response = self._llm.messages.create(
            model=self.model,
            max_tokens=self._settings.anthropic_max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_payload}],
        )
        return extract_response_text(response)


__all__ = ["PM_RETRIES", "PortfolioManagerAgent"]
