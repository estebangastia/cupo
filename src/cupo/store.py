"""Postgres storage layer.

This module is small on purpose. Every operation that mutates a counter is a
single SQL statement, because the moment a counter needs two round trips it
acquires a race condition:

    # the bug this file exists to avoid
    used = await db.fetchval("SELECT used FROM ... ")   # both requests read 49
    if used < limit:                                    # both decide there is room
        await db.execute("UPDATE ... SET used = $1", used + 1)   # both write 50

With one credit left and two concurrent requests, that grants two. Read the
statements below with that failure in mind; each is shaped to make it
impossible rather than unlikely.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import asyncpg

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# Atomic check-and-consume.
#
# The WHERE on DO UPDATE is the whole trick. Postgres takes a row-level lock on
# the conflicting row before evaluating it, and re-reads the row under that
# lock, so concurrent callers evaluate the predicate against each other's
# committed writes rather than against a stale snapshot.
#
# Returns one row when the units were granted, and zero rows when they were not.
# "No row" is the denial: there is no separate read to disagree with.
_CONSUME = """
INSERT INTO cupo_counters (customer_id, feature, window_start, used)
VALUES ($1, $2, $3, $4)
ON CONFLICT (customer_id, feature, window_start)
DO UPDATE SET used = cupo_counters.used + $4,
              updated_at = now()
WHERE cupo_counters.used + $4 <= $5
RETURNING used
"""

# Unmetered increment: used by track() where the limit is enforced elsewhere
# (or not at all, as with post-hoc token counts).
_INCREMENT = """
INSERT INTO cupo_counters (customer_id, feature, window_start, used)
VALUES ($1, $2, $3, $4)
ON CONFLICT (customer_id, feature, window_start)
DO UPDATE SET used = cupo_counters.used + $4,
              updated_at = now()
RETURNING used
"""

# Idempotent increment.
#
# The ledger insert and the counter increment are one statement. If the
# idempotency key already exists, the CTE yields no rows, the outer INSERT has
# nothing to insert, and the counter is untouched. A retried request is
# therefore a no-op at every layer, not "usually" a no-op.
_INCREMENT_IDEMPOTENT = """
WITH accepted AS (
    INSERT INTO cupo_events (idempotency_key, customer_id, feature, units, window_start)
    VALUES ($5, $1, $2, $4, $3)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING units
)
INSERT INTO cupo_counters (customer_id, feature, window_start, used)
SELECT $1, $2, $3, accepted.units FROM accepted
ON CONFLICT (customer_id, feature, window_start)
DO UPDATE SET used = cupo_counters.used + EXCLUDED.used,
              updated_at = now()
RETURNING used
"""

_READ = """
SELECT used FROM cupo_counters
WHERE customer_id = $1 AND feature = $2 AND window_start = $3
"""

_READ_ALL = """
SELECT feature, used FROM cupo_counters
WHERE customer_id = $1 AND window_start = $2
"""

_RESET = """
DELETE FROM cupo_counters WHERE customer_id = $1
"""


class PostgresStore:
    """Counter storage backed by Postgres.

    Accepts either a DSN (a pool is created and owned) or an existing asyncpg
    pool (borrowed, and left open on close). The second form matters when Cupo
    shares a database with the application, which is the whole point of
    embedded mode.
    """

    def __init__(
        self,
        dsn: str | None = None,
        pool: asyncpg.Pool | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ):
        if (dsn is None) == (pool is None):
            raise ValueError("provide exactly one of dsn or pool")
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        self._min_size = min_size
        self._max_size = max_size

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=self._min_size, max_size=self._max_size
            )

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("store is not connected; call connect() first")
        return self._pool

    async def migrate(self) -> None:
        """Create the tables. Idempotent, safe to run on every boot."""
        await self.pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    async def consume(
        self,
        customer_id: str,
        feature: str,
        window_start: datetime,
        units: int,
        limit: int,
    ) -> int | None:
        """Atomically consume `units` if doing so stays within `limit`.

        Returns the new total on success, or None if the request would have
        exceeded the limit (in which case nothing was written).
        """
        return await self.pool.fetchval(
            _CONSUME, customer_id, feature, window_start, units, limit
        )

    async def increment(
        self,
        customer_id: str,
        feature: str,
        window_start: datetime,
        units: int,
        idempotency_key: str | None = None,
    ) -> int | None:
        """Increment a counter without enforcing a limit.

        With an idempotency key, a repeated call returns None and changes
        nothing. Without one, every call increments.
        """
        if idempotency_key is None:
            return await self.pool.fetchval(
                _INCREMENT, customer_id, feature, window_start, units
            )
        return await self.pool.fetchval(
            _INCREMENT_IDEMPOTENT,
            customer_id,
            feature,
            window_start,
            units,
            idempotency_key,
        )

    async def used(
        self, customer_id: str, feature: str, window_start: datetime
    ) -> int:
        value = await self.pool.fetchval(_READ, customer_id, feature, window_start)
        return value or 0

    async def used_all(
        self, customer_id: str, window_start: datetime
    ) -> dict[str, int]:
        rows = await self.pool.fetch(_READ_ALL, customer_id, window_start)
        return {row["feature"]: row["used"] for row in rows}

    async def reset(self, customer_id: str) -> None:
        """Drop every counter for a customer. Intended for tests and support."""
        await self.pool.execute(_RESET, customer_id)
