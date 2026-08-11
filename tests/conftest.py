"""Test fixtures.

Tests need a real Postgres. Concurrency guarantees cannot be verified against a
mock or against SQLite, because the guarantee *is* the database's locking
behaviour. Point CUPO_TEST_DSN at a throwaway database:

    docker compose up -d
    CUPO_TEST_DSN=postgresql://cupo:cupo@localhost:5433/cupo_test pytest
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from cupo import Cupo

DSN = os.environ.get(
    "CUPO_TEST_DSN", "postgresql://cupo:cupo@localhost:5433/cupo_test"
)

PLANS = {
    "free": {"ai_chat": {"limit": 50, "window": "month"}},
    "single": {"ai_chat": {"limit": 1, "window": "month"}},
    "hundred": {"ai_chat": {"limit": 100, "window": "month"}},
    "tokens": {"ai_tokens": {"limit": 1_000, "window": "month"}},
    "unlimited": {"ai_chat": {"limit": "unlimited", "window": "month"}},
    "degrading": {
        "ai_chat": {"limit": 5, "window": "month", "on_limit": "degrade"}
    },
    "billing": {
        "ai_chat": {
            "limit": 5,
            "window": "month",
            "on_limit": "bill",
            "overage_price": 0.01,
        }
    },
    "gated": {"pdf_export": False, "ai_chat": True},
}


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: async test")


@pytest_asyncio.fixture
async def cupo():
    """A connected Cupo instance whose counters are wiped after each test.

    max_size is deliberately > 1: a single pooled connection would serialise
    the concurrency tests and make them pass for the wrong reason.
    """
    instance = Cupo(plans=PLANS, dsn=DSN, fail_open=False)
    instance.store._max_size = 20
    await instance.connect()

    yield instance

    await instance.store.pool.execute("TRUNCATE cupo_counters, cupo_events")
    await instance.close()


@pytest.fixture
def customer():
    return f"cust_{uuid.uuid4().hex[:12]}"
