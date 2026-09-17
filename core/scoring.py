"""Opinion-ladder scorer — the Master Plan's aggregation (pp.4-5) with one fix.

The plan's constants and three rules are law:
  Rule 1  Higher tiers cap lower tiers. Once a tier votes with validated
          confidence >= CAP_CONFIDENCE (0.7), opposite-direction votes from
          lower tiers are dampened by DAMPEN (0.25) and the score can never
          cross zero against the leader.
  Rule 2  No view, no vote. Neutral or absent agents drop out and their
          weight is excluded from the normalization sum.
  Rule 3  Hard red flags override everything. A red flag from the SEC
          Filings agent — the only red-flag-capable agent — sets the score
          to maximum bearish (-1.0).

Deviation from the plan's pseudocode (both documented, both required):
  * Tier-level aggregation. The plan's inner loop adds every agent's vote at
    full tier weight, double-counting agents within a tier. Votes accumulate
    per tier, are averaged, and the tier weight is applied once.
  * The result is clamped to [-1, 1]. The plan's formula is a weighted mean
    of tier votes, so it stays in [-1, 1] while every vote does — but
    validation multipliers above 1 can inflate a vote beyond 1.0; the return
    contract bounds the score to [-1, 1] regardless.
"""

from typing import Final, TypedDict

from core.schemas import Direction, Signal

TIERS: Final[tuple[tuple[str, ...], ...]] = (
    ("global_events",),
    ("macro_rates", "credit"),
    ("sec_filings", "earnings"),
    ("fundamentals", "sector_desk", "event_driven"),
    ("options_flow", "short_side", "alt_data"),
    ("sentiment",),
    ("technical", "stat_arb"),
)
WEIGHTS: Final[tuple[float, ...]] = (1.0, 0.85, 0.70, 0.55, 0.45, 0.40, 0.30)
DIRECTION: Final[dict[Direction, int]] = {
    Direction.BULLISH: 1,
    Direction.NEUTRAL: 0,
    Direction.BEARISH: -1,
}
CAP_CONFIDENCE: Final[float] = 0.7
DAMPEN: Final[float] = 0.25


class VoteRecord(TypedDict):
    """One agent's vote as recorded in the audit trail (pre-weight)."""

    agent: str
    tier: int
    direction: str
    strength: float
    confidence: float  # validated (post-multiplier)
    dampened: bool  # Rule 1 dampening applied
    vote: float  # direction * strength * validated confidence, post-dampen
    weight: float  # the tier weight that applies to this vote


class ScoreResult(TypedDict):
    """The aggregate() return shape (a plain dict, Postgres-ready)."""

    score: float  # in [-1, 1]
    led_by: str | None
    votes: list[VoteRecord]
    red_flag: bool


def aggregate(
    signals: dict[str, Signal],
    validation: dict[str, float] | None = None,
) -> ScoreResult:
    """Aggregate per-agent Signals into one ladder score.

    ``signals`` is keyed by agent name; ``validation`` maps agent names to
    per-agent confidence multipliers from the Quant desk (e.g. 0.8 for an
    agent whose signal type has a weak historical hit rate). Multipliers
    scale the confidence used both for the vote and for leader eligibility
    under the 0.7 cap.

    Agents absent from ``signals``, present but neutral, or unknown to the
    ladder cast no vote. On empty or all-neutral input the sentinel
    ``{"score": 0.0, "led_by": None, "votes": [], "red_flag": False}`` is
    returned. Rule 3 short-circuits with led_by set to
    ``"sec_filings (red flag)"`` and an empty votes list.
    """
    multipliers = validation or {}

    # Rule 3: hard red flags override everything.
    sec = signals.get("sec_filings")
    if sec is not None and sec.red_flag:
        return {
            "score": -1.0,
            "led_by": "sec_filings (red flag)",
            "votes": [],
            "red_flag": True,
        }

    total = 0.0
    weight_sum = 0.0
    ceiling_dir: int | None = None
    leader: str | None = None
    votes: list[VoteRecord] = []

    for tier_number, (tier_agents, weight) in enumerate(zip(TIERS, WEIGHTS, strict=True), start=1):
        tier_votes: list[float] = []
        for agent in tier_agents:
            signal = signals.get(agent)
            if signal is None or signal.signal is Direction.NEUTRAL:
                continue  # Rule 2: no view, no vote
            direction = DIRECTION[signal.signal]
            confidence = signal.confidence * multipliers.get(agent, 1.0)
            vote = float(direction * signal.strength * confidence)
            dampened = ceiling_dir is not None and direction != ceiling_dir
            if dampened:
                vote *= DAMPEN  # Rule 1
            tier_votes.append(vote)
            votes.append(
                VoteRecord(
                    agent=agent,
                    tier=tier_number,
                    direction=signal.signal.value,
                    strength=signal.strength,
                    confidence=confidence,
                    dampened=dampened,
                    vote=vote,
                    weight=weight,
                )
            )
            if ceiling_dir is None and confidence >= CAP_CONFIDENCE:
                ceiling_dir, leader = direction, agent
        # Tier-level aggregation: average the tier's votes, then weight once.
        if tier_votes:
            total += (sum(tier_votes) / len(tier_votes)) * weight
            weight_sum += weight

    score = total / weight_sum if weight_sum else 0.0
    if ceiling_dir is not None and score * ceiling_dir < 0:
        score = 0.0  # never flip past the leader
    score = max(-1.0, min(1.0, score))
    return {"score": round(score, 3), "led_by": leader, "votes": votes, "red_flag": False}
