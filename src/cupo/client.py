"""The Cupo client: check() and track()."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .plans import CheckResult, Entitlement, Plans
from .store import PostgresStore
from .windows import window_end, window_start

log = logging.getLogger("cupo")


class Cupo:
    """Entitlement checks and usage metering against your own Postgres.

        cupo = Cupo(dsn="postgresql://...", plans={
            "free": {"ai_chat": {"limit": 50, "window": "month"}},
            "pro":  {"ai_chat": {"limit": 5_000, "window": "month"}},
        })
        await cupo.connect()

        res = await cupo.check("cust_123", "ai_chat", plan="free")
        if not res.allowed:
            ...

    `fail_open` decides what happens when the database is unreachable. The
    default allows the request: for most products a brief window of unmetered
    usage is cheaper than an outage. Set it to False for features where an
    overshoot costs real money.
    """

    def __init__(
        self,
        plans: dict[str, dict[str, Any]],
        dsn: str | None = None,
        pool: asyncpg.Pool | None = None,
        *,
        fail_open: bool = True,
        upgrade_url: str | None = None,
    ):
        self.plans = Plans(plans)
        self.store = PostgresStore(dsn=dsn, pool=pool)
        self.fail_open = fail_open
        self.upgrade_url = upgrade_url

    async def connect(self, migrate: bool = True) -> None:
        await self.store.connect()
        if migrate:
            await self.store.migrate()

    async def close(self) -> None:
        await self.store.close()

    async def __aenter__(self) -> "Cupo":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ check

    async def check(
        self,
        customer_id: str,
        feature: str,
        plan: str,
        units: int = 1,
        *,
        now: datetime | None = None,
    ) -> CheckResult:
        """Atomically decide whether the customer may consume `units`, and
        consume them if so.

        This is check-and-consume in one operation, not a check followed by a
        consume. Two concurrent calls with a single credit remaining will see
        exactly one allowed.
        """
        if units < 0:
            raise ValueError("units must be non-negative")

        ent = self.plans.entitlement(plan, feature)
        now = now or datetime.now(timezone.utc)
        start = window_start(ent.window, now)
        resets = window_end(ent.window, now)

        if ent.unlimited:
            used = await self._safe(
                self.store.increment(customer_id, feature, start, units),
                default=0,
            )
            return CheckResult(
                allowed=True,
                feature=feature,
                used=used or 0,
                limit=None,
                resets_at=None,
            )

        # A single request larger than the entire allowance can never fit. It is
        # rejected here rather than in SQL, because the INSERT path of an upsert
        # has no existing row to test the predicate against.
        if units > ent.limit:
            return self._deny(ent, feature, used=await self._used(customer_id, feature, start), resets=resets,
                              reason="request_exceeds_limit")

        try:
            new_used = await self.store.consume(
                customer_id, feature, start, units, ent.limit
            )
        except Exception:
            log.exception("cupo: check failed for %s/%s", customer_id, feature)
            if self.fail_open:
                return CheckResult(
                    allowed=True,
                    feature=feature,
                    used=0,
                    limit=ent.limit,
                    resets_at=resets,
                    reason="fail_open",
                )
            return CheckResult(
                allowed=False,
                feature=feature,
                used=0,
                limit=ent.limit,
                resets_at=resets,
                reason="fail_closed",
                upgrade_url=self.upgrade_url,
            )

        if new_used is not None:
            return CheckResult(
                allowed=True,
                feature=feature,
                used=new_used,
                limit=ent.limit,
                resets_at=resets,
            )

        # Denied by the limit. Read the current value for the response body only;
        # the decision was already made atomically above.
        used = await self._used(customer_id, feature, start)

        if ent.on_limit == "degrade":
            await self._safe(
                self.store.increment(customer_id, feature, start, units), default=None
            )
            return CheckResult(
                allowed=True,
                feature=feature,
                used=used + units,
                limit=ent.limit,
                resets_at=resets,
                degraded=True,
                reason="over_limit_degraded",
            )

        if ent.on_limit == "bill":
            await self._safe(
                self.store.increment(customer_id, feature, start, units), default=None
            )
            return CheckResult(
                allowed=True,
                feature=feature,
                used=used + units,
                limit=ent.limit,
                resets_at=resets,
                overage=True,
                reason="over_limit_billed",
            )

        return self._deny(ent, feature, used=used, resets=resets, reason="plan_limit")

    # ------------------------------------------------------------------ track

    async def track(
        self,
        customer_id: str,
        feature: str,
        units: int = 1,
        *,
        plan: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> int | None:
        """Record usage after the fact, without enforcing a limit.

        This is the counterpart to check() for quantities only known once the
        work is done -- output tokens being the obvious case. Passing an
        idempotency key makes retries safe: the second call with the same key
        returns None and leaves the counter alone.
        """
        if units < 0:
            raise ValueError("units must be non-negative")

        window = "month"
        if plan is not None:
            window = self.plans.entitlement(plan, feature).window

        now = now or datetime.now(timezone.utc)
        start = window_start(window, now)

        return await self._safe(
            self.store.increment(
                customer_id, feature, start, units, idempotency_key=idempotency_key
            ),
            default=None,
        )

    # ------------------------------------------------------------------ usage

    async def usage(
        self, customer_id: str, plan: str, *, now: datetime | None = None
    ) -> dict[str, CheckResult]:
        """Current consumption for every feature in the plan, without consuming."""
        now = now or datetime.now(timezone.utc)
        out: dict[str, CheckResult] = {}
        for feature, ent in self.plans.features(plan).items():
            start = window_start(ent.window, now)
            used = await self._used(customer_id, feature, start)
            out[feature] = CheckResult(
                allowed=ent.unlimited or used < ent.limit,
                feature=feature,
                used=used,
                limit=ent.limit,
                resets_at=None if ent.unlimited else window_end(ent.window, now),
            )
        return out

    async def reset(self, customer_id: str) -> None:
        await self.store.reset(customer_id)

    # ----------------------------------------------------------------- helpers

    def _deny(
        self,
        ent: Entitlement,
        feature: str,
        used: int,
        resets: datetime,
        reason: str,
    ) -> CheckResult:
        return CheckResult(
            allowed=False,
            feature=feature,
            used=used,
            limit=ent.limit,
            resets_at=resets,
            reason=reason,
            upgrade_url=self.upgrade_url,
        )

    async def _used(self, customer_id: str, feature: str, start: datetime) -> int:
        return await self._safe(
            self.store.used(customer_id, feature, start), default=0
        )

    async def _safe(self, coro, default):
        try:
            return await coro
        except Exception:
            log.exception("cupo: storage error")
            if self.fail_open:
                return default
            raise
