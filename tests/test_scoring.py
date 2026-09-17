"""tests for core/scoring.py — the opinion ladder's three rules and its fix.

Expected values are computed by hand from the Master Plan's formula
(pp.4-5): vote = direction * strength * validated confidence, averaged
within a tier, weighted by the tier weight, normalized by the sum of
*voting* tiers' weights, then the no-flip and [-1, 1] rules.
"""

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from core.schemas import Signal
from core.scoring import CAP_CONFIDENCE, DAMPEN, TIERS, WEIGHTS, aggregate

# The Master Plan's example signal numbers (p.8) — the plan's exact figures.
PLAN_SIGNAL_JSON: Final[dict[str, Any]] = {
    "agent": "sec_filings",
    "tier": 3,
    "timestamp": "2026-09-17T13:30:00Z",
    "ticker": "XYZ",
    "signal": "bullish",
    "strength": 0.72,
    "confidence": 0.65,
    "horizon": "1-3 months",
    "red_flag": False,
    "thesis": "Cluster of 4 insider purchases totaling $3.1M after earnings",
    "evidence": [
        {
            "source": "Form 4",
            "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4",
            "excerpt": "Cluster of insider purchases totaling $3.1M after earnings",
        }
    ],
    "risks": ["Buying may be pre-planned"],
}

SENTINEL: Final[dict[str, Any]] = {
    "score": 0.0,
    "led_by": None,
    "votes": [],
    "red_flag": False,
}


def make_signal(
    agent: str,
    tier: int,
    direction: str,
    strength: float,
    confidence: float,
    **extra: Any,
) -> Signal:
    """A minimal valid signal for the given agent."""
    payload: dict[str, Any] = {
        "agent": agent,
        "tier": tier,
        "timestamp": "2026-09-17T13:30:00Z",
        "signal": direction,
        "strength": strength,
        "confidence": confidence,
        "horizon": "1-3 months",
        "thesis": f"{agent} speaking.",
        "evidence": [
            {"source": "Fixture", "url": "https://example.com/evidence", "excerpt": "..."}
        ],
        "risks": [],
    }
    payload.update(extra)
    return Signal.model_validate(payload)


def test_ladder_constants_match_the_plan() -> None:
    """The seven tiers, their weights, and the rule constants are the plan's."""
    assert TIERS == (
        ("global_events",),
        ("macro_rates", "credit"),
        ("sec_filings", "earnings"),
        ("fundamentals", "sector_desk", "event_driven"),
        ("options_flow", "short_side", "alt_data"),
        ("sentiment",),
        ("technical", "stat_arb"),
    )
    assert WEIGHTS == (1.0, 0.85, 0.70, 0.55, 0.45, 0.40, 0.30)
    assert CAP_CONFIDENCE == 0.7
    assert DAMPEN == 0.25


def test_empty_input_returns_documented_sentinel() -> None:
    """No signals at all is a documented zero score, not a crash."""
    assert aggregate({}) == SENTINEL


def test_all_neutral_returns_documented_sentinel() -> None:
    """Neutral agents drop out (Rule 2); all-neutral input is the same sentinel."""
    neutral = make_signal("global_events", 1, "neutral", 0.9, 0.9)
    neutral_credit = make_signal("credit", 2, "neutral", 0.9, 0.9)

    result = aggregate({"global_events": neutral, "credit": neutral_credit})

    assert result == SENTINEL


def test_red_flag_override_forces_max_bearish() -> None:
    """Rule 3: a sec_filings red flag scores -1.0 regardless of other votes."""
    sec = make_signal("sec_filings", 3, "bullish", 0.1, 0.1, red_flag=True)
    global_events = make_signal("global_events", 1, "bearish", 0.9, 0.9)

    result = aggregate({"sec_filings": sec, "global_events": global_events})

    assert result["score"] == -1.0
    assert result["led_by"] == "sec_filings (red flag)"
    assert result["red_flag"] is True
    assert result["votes"] == []


def test_red_flag_only_the_sec_agent_triggers() -> None:
    """Only the SEC Filings agent is red-flag capable; others score normally."""
    global_events = make_signal("global_events", 1, "bullish", 0.5, 0.8, red_flag=True)

    result = aggregate({"global_events": global_events})

    assert result["red_flag"] is False
    assert result["score"] == 0.4  # 1 * 0.5 * 0.8, sole voting tier (weight 1.0)
    assert result["led_by"] == "global_events"


def test_cap_and_dampen_on_the_plan_example_numbers() -> None:
    """Cap 0.7 + dampen 0.25 on the plan's p.8 signal.

    Scenario (the plan's own narrative — "Bearish on XYZ, led by Global
    Events"): global_events is bearish (strength 0.6, confidence 0.8 — above
    the 0.7 cap, so it leads) and the plan's exact p.8 sec_filings signal
    (bullish, strength 0.72, confidence 0.65) votes the other way.

      global_events vote: (-1) * 0.6 * 0.8        = -0.48   (weight 1.00)
      sec_filings vote:    (+1) * 0.72 * 0.65 * 0.25 = +0.117 (dampened, weight 0.70)

      score = (-0.48 + 0.117 * 0.70) / (1.00 + 0.70) = -0.234
    """
    global_events = make_signal("global_events", 1, "bearish", 0.6, 0.8)
    sec = Signal.model_validate(PLAN_SIGNAL_JSON)

    result = aggregate({"global_events": global_events, "sec_filings": sec})

    assert result["score"] == -0.234
    assert result["led_by"] == "global_events"
    assert result["red_flag"] is False
    sec_vote = result["votes"][1]
    assert sec_vote["dampened"] is True
    assert sec_vote["vote"] == pytest.approx(DAMPEN * 0.72 * 0.65)
    assert sec_vote["weight"] == 0.70
    assert sec_vote["tier"] == 3


def test_confidence_cap_boundary() -> None:
    """Exactly CAP_CONFIDENCE leads; just below does not (plan Rule 1)."""
    leads = make_signal("global_events", 1, "bullish", 1.0, CAP_CONFIDENCE)
    no_lead = make_signal("global_events", 1, "bullish", 1.0, 0.69)

    assert aggregate({"global_events": leads})["led_by"] == "global_events"
    assert aggregate({"global_events": no_lead})["led_by"] is None


def test_no_flip_past_the_leader() -> None:
    """Rule 1: the score can never cross zero against the leader.

    global_events: bearish, strength 0.05, confidence 0.9 -> vote -0.045, leads.
    sentiment (w 0.40):  bullish, 1.0/1.0 -> dampened 0.25 * 0.40 = +0.10
    technical (w 0.30):  bullish, 1.0/1.0 -> dampened 0.25 * 0.30 = +0.075

    Raw score = (-0.045 + 0.10 + 0.075) / 1.70 = +0.076 — against the bearish
    leader, so the no-flip rule clamps it to 0.0.
    """
    result = aggregate(
        {
            "global_events": make_signal("global_events", 1, "bearish", 0.05, 0.9),
            "sentiment": make_signal("sentiment", 6, "bullish", 1.0, 1.0),
            "technical": make_signal("technical", 7, "bullish", 1.0, 1.0),
        }
    )

    assert result["score"] == 0.0
    assert result["led_by"] == "global_events"


def test_no_flip_does_not_clamp_scores_on_the_leaders_side() -> None:
    """Dampening alone (no zero-crossing) leaves the score untouched."""
    result = aggregate(
        {
            "global_events": make_signal("global_events", 1, "bearish", 0.6, 0.8),
            "technical": make_signal("technical", 7, "bullish", 0.5, 0.9),
        }
    )

    # global_events: -0.48 * 1.0; technical: 0.45 * 0.25 * 0.30 = 0.03375
    # score = (-0.48 + 0.03375) / 1.30 = -0.343... — negative, so no clamp.
    assert result["score"] == -0.343
    assert result["led_by"] == "global_events"


def test_equal_tier_agents_average() -> None:
    """Two same-direction tier-2 agents average before the 0.85 weight applies."""
    result = aggregate(
        {
            "macro_rates": make_signal("macro_rates", 2, "bullish", 0.8, 0.6),
            "credit": make_signal("credit", 2, "bullish", 0.6, 0.6),
        }
    )

    # Tier 2 mean vote = (0.48 + 0.36) / 2 = 0.42; sole voting tier -> score 0.42.
    assert result["score"] == 0.42
    assert result["led_by"] is None  # neither clears the 0.7 cap


def test_equal_tier_agents_averaged_opposing_cancel() -> None:
    """Two opposing same-tier votes average to zero; the tier weight applies once."""
    result = aggregate(
        {
            "macro_rates": make_signal("macro_rates", 2, "bullish", 1.0, 0.5),
            "credit": make_signal("credit", 2, "bearish", 1.0, 0.5),
        }
    )

    assert result["score"] == 0.0
    assert result["led_by"] is None
    assert len(result["votes"]) == 2


def test_tier_level_aggregation_fix() -> None:
    """Within-tier votes aggregate before the tier weight applies.

    macro_rates 0.48 and credit 0.36 (both tier 2), technical -0.2 (tier 7):
      fixed:   (0.42 * 0.85 - 0.2 * 0.30) / (0.85 + 0.30) = 0.297 / 1.15 = 0.258
      plan's double-counted loop: (0.48*0.85 + 0.36*0.85 - 0.2*0.30) / 2.0 = 0.327
    The plan's inner loop would over-weight tier 2; the fix weights it once.
    """
    result = aggregate(
        {
            "macro_rates": make_signal("macro_rates", 2, "bullish", 0.8, 0.6),
            "credit": make_signal("credit", 2, "bullish", 0.6, 0.6),
            "technical": make_signal("technical", 7, "bearish", 0.5, 0.4),
        }
    )

    assert result["score"] == 0.258
    assert result["score"] != 0.327  # the double-counted value the plan would produce


def test_neutral_weight_excluded_from_normalization() -> None:
    """Rule 2: absent tiers' weights are excluded from the denominator.

    Only technical votes (bullish, strength 0.2, confidence 0.9): tier vote
    0.18, contribution 0.18 * 0.30, weight_sum = 0.30 -> score 0.18 (the
    single voting tier's weight cancels). Had every tier's weight (7.0
    total) been counted instead, the score would be 0.0077.
    """
    result = aggregate({"technical": make_signal("technical", 7, "bullish", 0.2, 0.9)})

    assert result["score"] == 0.18
    assert result["led_by"] == "technical"


def test_higher_tier_leads_when_both_confident() -> None:
    """The highest tier clearing the cap leads, even when a lower tier is louder."""
    result = aggregate(
        {
            "global_events": make_signal("global_events", 1, "bearish", 0.5, 0.8),
            "technical": make_signal("technical", 7, "bullish", 1.0, 0.95),
        }
    )

    assert result["led_by"] == "global_events"
    # global_events: -0.4; technical: 0.95 * 0.25 * 0.30 = 0.07125
    # score = (-0.4 + 0.07125) / 1.30 = -0.253...
    assert result["score"] == -0.253


def test_validation_multipliers_scale_confidence() -> None:
    """Quant-desk multipliers scale both the vote and cap eligibility."""
    base = {
        "global_events": make_signal("global_events", 1, "bullish", 0.5, 0.9),
        "technical": make_signal("technical", 7, "bullish", 1.0, 0.9),
    }

    dampened_conf = aggregate(base, validation={"technical": 0.5})
    plain = aggregate(base)

    # technical conf 0.9*0.5 = 0.45 -> vote 0.45; total (0.45 + 0.135) / 1.30 = 0.45
    assert dampened_conf["score"] == 0.45
    assert dampened_conf["led_by"] == "global_events"
    # without validation technical votes at full confidence: 0.72 / 1.30 = 0.554
    assert plain["score"] == 0.554


def test_validation_multiplier_can_demote_the_leader() -> None:
    """A multiplier that drops the top tier below the cap hands the lead down."""
    result = aggregate(
        {
            "global_events": make_signal("global_events", 1, "bullish", 0.5, 0.8),
            "technical": make_signal("technical", 7, "bullish", 0.5, 0.9),
        },
        validation={"global_events": 0.5},
    )

    # global_events conf 0.4 < 0.7 -> no ceiling; technical (0.9) leads.
    assert result["led_by"] == "technical"
    assert result["score"] == 0.258  # (0.2 + 0.45*0.30) / 1.30


def test_score_clamped_to_contract_bounds() -> None:
    """The return contract bounds score to [-1, 1].

    Validation multipliers above 1 can inflate a vote past 1.0: a lone
    technical vote at 2.0x scores (2.0 * 0.30) / 0.30 = 2.0 raw; the clamp
    returns the contract's maximum magnitude of 1.0.
    """
    bullish = aggregate(
        {"technical": make_signal("technical", 7, "bullish", 1.0, 1.0)},
        validation={"technical": 2.0},
    )
    bearish = aggregate(
        {"technical": make_signal("technical", 7, "bearish", 1.0, 1.0)},
        validation={"technical": 2.0},
    )

    assert bullish["score"] == 1.0
    assert bearish["score"] == -1.0


def test_unknown_agents_are_ignored() -> None:
    """Signals from agents outside the ladder cast no vote."""
    result = aggregate({"mystery_agent": make_signal("mystery_agent", 1, "bullish", 1.0, 1.0)})

    assert result == SENTINEL


def test_votes_audit_trail_records_dampening_and_weights() -> None:
    """The votes list is the audit trail the PM brief and Postgres cite."""
    result = aggregate(
        {
            "global_events": make_signal("global_events", 1, "bearish", 0.6, 0.8),
            "sec_filings": Signal.model_validate(PLAN_SIGNAL_JSON),
        }
    )

    leader_vote, dampened_vote = result["votes"]
    assert leader_vote["agent"] == "global_events"
    assert leader_vote["tier"] == 1
    assert leader_vote["direction"] == "bearish"
    assert leader_vote["dampened"] is False
    assert leader_vote["vote"] == pytest.approx(-0.48)
    assert dampened_vote["agent"] == "sec_filings"
    assert dampened_vote["confidence"] == 0.65  # unmodified by dampening
    assert dampened_vote["dampened"] is True
    assert dampened_vote["vote"] == pytest.approx(0.117)


def test_scoring_is_tz_agnostic_but_timestamps_still_coerce() -> None:
    """Scores do not depend on timestamps, and naive stamps still coerce to UTC."""
    signal = make_signal(
        "global_events",
        1,
        "bullish",
        0.5,
        0.8,
        timestamp=datetime(2026, 9, 17, 9, 0),  # naive
    )

    result = aggregate({"global_events": signal})

    assert result["score"] == 0.4
    assert signal.timestamp.tzinfo is UTC
