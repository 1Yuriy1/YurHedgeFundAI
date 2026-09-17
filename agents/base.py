"""Agent base — the collect → analyze contract shared by every analyst.

An agent receives everything it needs injected: an LLM client shaped like the
Anthropic SDK client (a structural protocol, so tests stub it), its connector
instances, and its tier/model from ``core.config.Settings``. Two steps:

* ``collect``  — pull raw data from connectors. Pure data, no LLM. Connector
  failures degrade to logged gaps, never exceptions across the boundary.
* ``analyze``  — one Sonnet-family LLM call: raw data in, schema-validated
  ``Signal`` objects out. Malformed LLM output is retried ONCE, then the
  signal is dropped with a structured warning — a dropped signal is better
  than an invalid one ("no view, no vote", Master Plan p.4).

Versioned system prompts live as markdown under ``prompts/``; every prompt
embeds the Signal JSON schema and the evidence rule ("every signal cites at
least one URL-backed source; cite figures exactly as returned by the data").
"""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, TypedDict

from core.config import Settings
from core.schemas import Direction, Evidence, Signal
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# prompts/ sits beside the agents package: repo checkout (agents/../prompts)
# and installed wheel (site-packages/prompts, force-included) both resolve.
PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "prompts"

RAW_PREVIEW_CHARS: Final[int] = 200


class PromptNotFoundError(FileNotFoundError):
    """A versioned prompt file is missing from prompts/."""


class LLMOutputError(ValueError):
    """The LLM response could not be parsed into schema-valid signals."""


# ── LLM client protocol ─────────────────────────────────────────────────────
# A structural subset of the Anthropic SDK client: ``client.messages.create``
# with the keyword arguments the agents use. The real client satisfies it
# duck-typed at runtime; tests pass a stub, so the anthropic package is not a
# dependency of this layer.


class LLMTextBlock(Protocol):
    """Subset of an SDK content block: the text we read."""

    @property
    def text(self) -> str: ...


class LLMResponse(Protocol):
    """Subset of the SDK's Message: a list of content blocks."""

    @property
    def content(self) -> Sequence[LLMTextBlock]: ...


class LLMMessageParam(TypedDict):
    """One chat message sent to the model."""

    role: str
    content: str


class LLMCreatePort(Protocol):
    """``client.messages.create`` with the kwargs the agents use."""

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[LLMMessageParam],
    ) -> LLMResponse: ...


class LLMClient(Protocol):
    """Structural subset of the Anthropic SDK client.

    Members are read-only properties on purpose: protocol *variables* are
    checked invariantly, which no real client (or test stub) with a concrete
    ``messages`` implementation could satisfy.
    """

    @property
    def messages(self) -> LLMCreatePort: ...


def extract_response_text(response: LLMResponse) -> str:
    """Concatenate the text blocks of a response (non-text blocks ignored)."""
    parts = [block.text for block in response.content if isinstance(block.text, str)]
    return "".join(parts).strip()


# ── Prompt loading ──────────────────────────────────────────────────────────


def load_prompt(filename: str, *, prompts_dir: Path = PROMPTS_DIR) -> str:
    """Load a versioned prompt from prompts/; missing or empty fails loudly."""
    path = prompts_dir / filename
    if not path.is_file():
        raise PromptNotFoundError(f"prompt file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise LLMOutputError(f"prompt file is empty: {path}")
    return text


def compact_json(payload: Any) -> str:
    """Stable JSON rendering of a collect() payload for the user message."""
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


# ── LLM output parsing ──────────────────────────────────────────────────────


def strip_code_fence(text: str) -> str:
    """Remove a single wrapping markdown code fence, if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


class AnalystSignalPayload(BaseModel):
    """The analyst-owned subset of Signal.

    ``agent``, ``tier``, and ``timestamp`` are stamped by the platform so the
    model cannot spoof another agent's identity or a bogus tier; prompts show
    only these fields.
    """

    ticker: str | None = None
    signal: Direction
    strength: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    horizon: str
    red_flag: bool = False
    thesis: str
    evidence: list[Evidence] = Field(min_length=1)
    risks: list[str] = []


def parse_llm_signals(raw: str) -> list[AnalystSignalPayload]:
    """Parse a JSON array (or single object) of analyst signals.

    Any structural or schema failure raises ``LLMOutputError`` so the caller
    can run its single retry — validity is enforced mechanically, per the
    evidence rule.
    """
    try:
        decoded = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"not valid JSON: {exc}") from exc
    if isinstance(decoded, dict):
        items: list[Any] = [decoded]
    elif isinstance(decoded, list) and all(isinstance(item, dict) for item in decoded):
        # An empty array is legitimate silence ("no view, no vote"), not a
        # malformed response.
        items = decoded
    else:
        raise LLMOutputError("expected a JSON object or an array of objects")
    payloads: list[AnalystSignalPayload] = []
    for position, item in enumerate(items):
        try:
            payloads.append(AnalystSignalPayload.model_validate(item))
        except ValueError as exc:
            raise LLMOutputError(f"invalid signal at position {position}: {exc}") from exc
    return payloads


# ── BaseAgent ───────────────────────────────────────────────────────────────


class BaseAgent(ABC):
    """One analyst: connectors in (collect), validated Signals out (analyze)."""

    name: ClassVar[str] = "agent"
    # Versioned markdown under prompts/, e.g. "macro_rates_v1.md".
    prompt_filename: ClassVar[str]

    def __init__(self, *, llm: LLMClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings
        # Locked decision 2: model IDs live in config, never hardcoded.
        self.model: str = settings.anthropic_model_analyst
        self.tier: int = self._resolve_tier(settings)
        # A missing prompt fails at construction, not mid-pipeline.
        self.system_prompt: str = load_prompt(self.prompt_filename)

    def _resolve_tier(self, settings: Settings) -> int:
        """The opinion-ladder tier comes from config, not from the class."""
        tier = settings.agent_tiers.get(self.name)
        if tier is None:
            raise ValueError(
                f"no tier configured for agent {self.name!r}; "
                f'set AGENT_TIERS="{self.name}:<tier>,..." or add it to config'
            )
        return tier

    @abstractmethod
    def collect(self, tickers: list[str]) -> dict[str, Any]:
        """Pull raw data from this agent's connectors. Pure data, no LLM."""

    @abstractmethod
    def analyze(self, tickers: list[str], data: dict[str, Any]) -> list[Signal]:
        """One LLM call: raw data in, schema-validated Signals out."""

    def run(self, tickers: list[str]) -> list[Signal]:
        """The full analyst step: collect, then analyze."""
        return self.analyze(tickers, self.collect(tickers))

    # ── Shared LLM machinery ────────────────────────────────────────────

    def request_signals(self, user_payload: str) -> list[Signal]:
        """One analyst LLM call; malformed output is retried once, then dropped.

        Every returned Signal is schema-valid and stamped with this agent's
        identity (name, tier, UTC timestamp). Failure paths log structured
        warnings — ``llm_output_rejected`` on the first attempt, then
        ``signal_dropped`` when the retry also fails — and return [].
        """
        payloads: list[AnalystSignalPayload] = []
        for attempt in (1, 2):
            raw = self._call_llm(user_payload)
            try:
                payloads = parse_llm_signals(raw)
            except LLMOutputError as exc:
                logger.warning(
                    "llm_output_rejected; retrying once" if attempt == 1 else "signal_dropped",
                    extra={
                        "agent": self.name,
                        "attempt": attempt,
                        "error": str(exc),
                        "raw_preview": raw[:RAW_PREVIEW_CHARS],
                    },
                )
                continue
            break
        return self._materialize(payloads)

    def _call_llm(self, user_payload: str) -> str:
        """The single Sonnet-family call every analyst makes."""
        response = self._llm.messages.create(
            model=self.model,
            max_tokens=self._settings.anthropic_max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_payload}],
        )
        return extract_response_text(response)

    def _materialize(self, payloads: list[AnalystSignalPayload]) -> list[Signal]:
        """Stamp platform-owned fields; returns only fully valid Signals."""
        now = datetime.now(UTC)
        return [
            Signal(
                agent=self.name,
                tier=self.tier,
                timestamp=now,
                **payload.model_dump(),
            )
            for payload in payloads
        ]


__all__ = [
    "AnalystSignalPayload",
    "BaseAgent",
    "LLMClient",
    "LLMOutputError",
    "PROMPTS_DIR",
    "PromptNotFoundError",
    "compact_json",
    "extract_response_text",
    "load_prompt",
    "parse_llm_signals",
    "strip_code_fence",
]
