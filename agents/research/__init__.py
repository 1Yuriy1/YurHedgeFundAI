"""Research analyst agents: Macro & Rates (T2), SEC Filings (T3), Fundamentals (T4)."""

from agents.research.fundamentals import FundamentalsAgent
from agents.research.macro_rates import MacroRatesAgent
from agents.research.sec_filings import RED_FLAG_PHRASES, SecFilingsAgent

__all__ = [
    "RED_FLAG_PHRASES",
    "FundamentalsAgent",
    "MacroRatesAgent",
    "SecFilingsAgent",
]
