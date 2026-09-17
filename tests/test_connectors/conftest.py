"""Shared offline-test plumbing: a fake clock, fakeredis, and fixture loading.

No test in this suite touches the network — HTTP is intercepted by respx and
Redis is faked, per the spec's offline-CI decision.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from core.config import Settings
from fakeredis import FakeRedis

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

TEST_EDGAR_USER_AGENT = "YuriyResearch/YurHedgeFundAI yuriy@example.com"


class FakeClock:
    """Deterministic clock: sleep() records the wait and advances the time."""

    def __init__(self) -> None:
        self._now = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=False)


def load_fixture(name: str) -> str:
    """Return a recorded fixture body as text."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_json(name: str) -> Any:
    return json.loads(load_fixture(name))


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests: connector keys pre-set unless a test overrides."""
    defaults: dict[str, Any] = {
        "fred_api_key": "test-fred-key",
        "edgar_user_agent": TEST_EDGAR_USER_AGENT,
        "fmp_api_key": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)
