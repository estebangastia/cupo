"""The tests this project exists to pass.

A usage limiter is easy to write and hard to write correctly. The gap between
the two is almost entirely concurrency: the naive implementation reads a
counter, compares it to a limit, and writes the counter back, which grants two
requests the same last credit whenever they arrive together.

These tests fire many requests at once at a customer with a known allowance and
assert on the exact total. They are the reason to trust the counters; if they
pass on your hardware, the guarantee holds there too.

One caveat worth stating plainly: the two-request test below is the readable
illustration, not the reliable detector. Races are probabilistic, and a naive
read-modify-write implementation passes it perhaps half the time. The
high-concurrency tests are the ones that fail deterministically -- measured
against a deliberately naive implementation, `test_limit_is_never_exceeded_under_load`
granted 200 requests against a limit of 50 and left the stored counter at 21.
If you are adapting these tests for your own code, keep the loud ones.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


PLANS = {
    "free": {"ai_chat": {"limit": 50, "window": "month"}},
    "single": {"ai_chat": {"limit": 1, "window": "month"}},
    "hundred": {"ai_chat": {"limit": 100, "window": "month"}},
}


async def test_one_credit_two_concurrent_requests(cupo):
    """The canonical case. One credit, two simultaneous requests, one winner."""
    results = await asyncio.gather(
        cupo.check("cust_duel", "ai_chat", plan="single"),
        cupo.check("cust_duel", "ai_chat", plan="single"),
    )

    allowed = [r for r in results if r.allowed]
    assert len(allowed) == 1, "exactly one request may consume the last credit"
    assert allowed[0].used == 1

    denied = [r for r in results if not r.allowed]
    assert len(denied) == 1
    assert denied[0].reason == "plan_limit"


async def test_one_credit_fifty_concurrent_requests(cupo):
    """Same guarantee under real contention rather than a two-way race."""
    results = await asyncio.gather(
        *(cupo.check("cust_stampede", "ai_chat", plan="single") for _ in range(50))
    )

    assert sum(r.allowed for r in results) == 1
    assert await cupo.store.used(
        "cust_stampede", "ai_chat", _month_start()
    ) == 1


async def test_limit_is_never_exceeded_under_load(cupo):
    """200 concurrent requests against a limit of 50: exactly 50 pass."""
    results = await asyncio.gather(
        *(cupo.check("cust_load", "ai_chat", plan="free") for _ in range(200))
    )

    allowed = sum(r.allowed for r in results)
    assert allowed == 50, f"expected exactly 50 grants, got {allowed}"

    stored = await cupo.store.used("cust_load", "ai_chat", _month_start())
    assert stored == 50, "the stored counter must agree with the grants issued"


async def test_multi_unit_requests_do_not_overshoot(cupo):
    """Requests consuming several units at once must not straddle the limit.

    A limit of 100 with 60 concurrent requests of 3 units each: at most 33 can
    be granted (99 units), because a 34th would reach 102.
    """
    results = await asyncio.gather(
        *(
            cupo.check("cust_multi", "ai_chat", plan="hundred", units=3)
            for _ in range(60)
        )
    )

    allowed = sum(r.allowed for r in results)
    stored = await cupo.store.used("cust_multi", "ai_chat", _month_start())

    assert stored <= 100, f"limit breached: {stored} units consumed against 100"
    assert stored == allowed * 3
    assert allowed == 33


async def test_customers_are_isolated(cupo):
    """One customer exhausting a limit must not affect another."""
    await asyncio.gather(
        *(cupo.check("cust_a", "ai_chat", plan="single") for _ in range(10)),
        *(cupo.check("cust_b", "ai_chat", plan="single") for _ in range(10)),
    )

    assert await cupo.store.used("cust_a", "ai_chat", _month_start()) == 1
    assert await cupo.store.used("cust_b", "ai_chat", _month_start()) == 1


async def test_denied_requests_consume_nothing(cupo):
    """A rejection must leave the counter untouched, not partially applied."""
    for _ in range(50):
        assert (await cupo.check("cust_exact", "ai_chat", plan="free")).allowed

    before = await cupo.store.used("cust_exact", "ai_chat", _month_start())
    for _ in range(20):
        res = await cupo.check("cust_exact", "ai_chat", plan="free")
        assert not res.allowed

    after = await cupo.store.used("cust_exact", "ai_chat", _month_start())
    assert before == after == 50


def _month_start():
    from datetime import datetime, timezone

    from cupo.windows import window_start

    return window_start("month", datetime.now(timezone.utc))
