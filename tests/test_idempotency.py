"""Retries must not double-count.

Anything that meters usage lives behind at least one retry: an HTTP client with
a backoff, a task queue with at-least-once delivery, a user double-clicking. If
track() is not idempotent, every one of those quietly overcharges the customer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from cupo.windows import window_start

pytestmark = pytest.mark.asyncio


def _now_month():
    return window_start("month", datetime.now(timezone.utc))


async def test_same_key_counts_once(cupo, customer):
    first = await cupo.track(customer, "ai_tokens", 1_500, idempotency_key="req_1")
    second = await cupo.track(customer, "ai_tokens", 1_500, idempotency_key="req_1")

    assert first == 1_500
    assert second is None, "a repeated key must report that nothing was applied"
    assert await cupo.store.used(customer, "ai_tokens", _now_month()) == 1_500


async def test_concurrent_retries_of_one_key(cupo, customer):
    """The realistic shape: a retry storm firing the same key at once."""
    results = await asyncio.gather(
        *(
            cupo.track(customer, "ai_tokens", 800, idempotency_key="req_storm")
            for _ in range(40)
        )
    )

    applied = [r for r in results if r is not None]
    assert len(applied) == 1, "exactly one of 40 identical retries may apply"
    assert await cupo.store.used(customer, "ai_tokens", _now_month()) == 800


async def test_different_keys_accumulate(cupo, customer):
    for i in range(5):
        await cupo.track(customer, "ai_tokens", 100, idempotency_key=f"req_{i}")

    assert await cupo.store.used(customer, "ai_tokens", _now_month()) == 500


async def test_no_key_means_every_call_counts(cupo, customer):
    """Opting out of idempotency is allowed, and must behave predictably."""
    for _ in range(5):
        await cupo.track(customer, "ai_tokens", 100)

    assert await cupo.store.used(customer, "ai_tokens", _now_month()) == 500


async def test_keys_are_global_not_per_feature(cupo, customer):
    """An idempotency key identifies a request, not a request-feature pair.

    Reusing one key across two features is a caller bug. Cupo treats the second
    call as a duplicate rather than silently accepting it, because the
    alternative hides the mistake until it shows up on an invoice.
    """
    await cupo.track(customer, "ai_tokens", 100, idempotency_key="shared")
    second = await cupo.track(customer, "ai_chat", 1, idempotency_key="shared")

    assert second is None
    assert await cupo.store.used(customer, "ai_chat", _now_month()) == 0


async def test_ledger_records_accepted_events_only(cupo, customer):
    await cupo.track(customer, "ai_tokens", 250, idempotency_key="ledger_1")
    await cupo.track(customer, "ai_tokens", 250, idempotency_key="ledger_1")
    await cupo.track(customer, "ai_tokens", 250, idempotency_key="ledger_2")

    rows = await cupo.store.pool.fetch(
        "SELECT idempotency_key, units FROM cupo_events WHERE customer_id = $1"
        " ORDER BY idempotency_key",
        customer,
    )
    assert [r["idempotency_key"] for r in rows] == ["ledger_1", "ledger_2"]
    assert await cupo.store.used(customer, "ai_tokens", _now_month()) == 500
