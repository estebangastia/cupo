"""Replace the atomic counter with the naive version, for CI only.

A test suite that has never failed proves nothing. The `mutation` job in
.github/workflows/tests.yml runs this script and then asserts the atomicity
tests go red -- if they stay green against a knowingly broken counter, they are
not testing what they claim to test and CI fails.

This is only ever run inside a disposable CI checkout.
"""

import sys
from pathlib import Path

STORE = Path(__file__).resolve().parents[2] / "src" / "cupo" / "store.py"

NAIVE_CONSUME = '''
    async def consume(
        self,
        customer_id: str,
        feature: str,
        window_start: datetime,
        units: int,
        limit: int,
    ) -> int | None:
        """DELIBERATELY BROKEN: read-modify-write, injected by CI."""
        async with self.pool.acquire() as con:
            used = await con.fetchval(_READ, customer_id, feature, window_start) or 0
            if used + units > limit:
                return None
            await con.execute(
                """
                INSERT INTO cupo_counters (customer_id, feature, window_start, used)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (customer_id, feature, window_start)
                DO UPDATE SET used = $4
                """,
                customer_id,
                feature,
                window_start,
                used + units,
            )
            return used + units
'''

source = STORE.read_text(encoding="utf-8")

start = source.index("    async def consume(")
end = source.index("    async def increment(")

STORE.write_text(source[:start] + NAIVE_CONSUME.lstrip("\n") + "\n" + source[end:], encoding="utf-8")
print("Patched store.py with the naive read-modify-write implementation.")
