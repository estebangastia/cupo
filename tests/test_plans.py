"""Plan parsing, window arithmetic and the on_limit policies."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cupo import PlanError, Plans
from cupo.windows import InvalidWindow, window_end, window_start




# ---------------------------------------------------------------- plan parsing


def test_bad_plan_fails_at_startup_not_at_runtime():
    """A malformed plan must crash on boot rather than at 3am under load."""
    with pytest.raises(PlanError):
        Plans({"free": {"ai_chat": {"window": "month"}}})  # no limit

    with pytest.raises(PlanError):
        Plans({"free": {"ai_chat": {"limit": -1}}})

    with pytest.raises(InvalidWindow):
        Plans({"free": {"ai_chat": {"limit": 10, "window": "fortnight"}}})

    with pytest.raises(PlanError):
        # on_limit="bill" without a price would produce uninvoiceable overage
        Plans({"pro": {"ai_chat": {"limit": 10, "on_limit": "bill"}}})


def test_boolean_shorthand():
    plans = Plans({"free": {"pdf_export": False, "ai_chat": True}})

    assert plans.entitlement("free", "pdf_export").limit == 0
    assert plans.entitlement("free", "ai_chat").unlimited


def test_unknown_plan_or_feature_is_explicit():
    plans = Plans({"free": {"ai_chat": {"limit": 1}}})

    with pytest.raises(PlanError, match="unknown plan"):
        plans.entitlement("enterprise", "ai_chat")
    with pytest.raises(PlanError, match="does not define feature"):
        plans.entitlement("free", "video_export")


# --------------------------------------------------------------------- windows


def test_window_boundaries():
    now = datetime(2026, 3, 17, 14, 35, 22, tzinfo=timezone.utc)

    assert window_start("month", now) == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert window_start("day", now) == datetime(2026, 3, 17, tzinfo=timezone.utc)
    assert window_start("hour", now) == datetime(
        2026, 3, 17, 14, tzinfo=timezone.utc
    )
    assert window_end("month", now) == datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_december_rolls_into_january():
    """The off-by-one that breaks limits once a year."""
    now = datetime(2026, 12, 14, tzinfo=timezone.utc)
    assert window_end("month", now) == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_naive_datetimes_are_rejected():
    with pytest.raises(InvalidWindow):
        window_start("month", datetime(2026, 3, 17))


# ------------------------------------------------------------------- behaviour


async def test_counters_are_scoped_to_their_window(cupo, customer):
    """Consumption in one month must not count against the next."""
    march = datetime(2026, 3, 20, tzinfo=timezone.utc)
    april = datetime(2026, 4, 2, tzinfo=timezone.utc)

    for _ in range(50):
        assert (await cupo.check(customer, "ai_chat", "free", now=march)).allowed

    assert not (await cupo.check(customer, "ai_chat", "free", now=march)).allowed
    # New window, fresh allowance, with no cron job having run in between.
    assert (await cupo.check(customer, "ai_chat", "free", now=april)).allowed


async def test_degrade_allows_but_flags(cupo, customer):
    for _ in range(5):
        assert not (await cupo.check(customer, "ai_chat", "degrading")).degraded

    res = await cupo.check(customer, "ai_chat", "degrading")
    assert res.allowed and res.degraded
    assert res.reason == "over_limit_degraded"


async def test_bill_allows_and_marks_overage(cupo, customer):
    for _ in range(5):
        await cupo.check(customer, "ai_chat", "billing")

    res = await cupo.check(customer, "ai_chat", "billing")
    assert res.allowed and res.overage
    assert res.used == 6


async def test_unlimited_still_meters(cupo, customer):
    """Unlimited means no ceiling, not no visibility."""
    for _ in range(10):
        res = await cupo.check(customer, "ai_chat", "unlimited")
        assert res.allowed and res.limit is None

    usage = await cupo.usage(customer, "unlimited")
    assert usage["ai_chat"].used == 10


async def test_request_larger_than_the_whole_allowance_is_denied(cupo, customer):
    res = await cupo.check(customer, "ai_chat", "free", units=51)

    assert not res.allowed
    assert res.reason == "request_exceeds_limit"
    assert res.used == 0, "a denied request must not partially consume"


async def test_remaining_and_bool_protocol(cupo, customer):
    res = await cupo.check(customer, "ai_chat", "free", units=10)

    assert bool(res) is True
    assert res.remaining == 40


async def test_usage_reports_without_consuming(cupo, customer):
    await cupo.check(customer, "ai_chat", "free", units=7)

    first = await cupo.usage(customer, "free")
    second = await cupo.usage(customer, "free")

    assert first["ai_chat"].used == second["ai_chat"].used == 7
