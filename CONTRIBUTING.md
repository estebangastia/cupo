# Running the tests

The suite needs a real Postgres. Concurrency guarantees cannot be verified
against a mock or against SQLite, because the guarantee *is* the database's
locking behaviour.

```bash
docker compose up -d
pip install -e ".[dev]"
pytest
```

To point at a different database:

```bash
CUPO_TEST_DSN=postgresql://user:pass@host:5432/dbname pytest
```

## What the suite covers

| File | Guarantee |
|---|---|
| `tests/test_atomicity.py` | A limit is never exceeded, at any level of concurrency |
| `tests/test_idempotency.py` | A retried `track()` counts once |
| `tests/test_metering.py` | Streaming responses are metered when the stream closes, across both the Anthropic context-manager shape and the OpenAI-compatible iterator shape (including Groq's vendor `x_groq.usage` field) |
| `tests/test_plans.py` | Plan parsing fails at startup; windows roll over without a cron job |

## Verifying the tests actually catch the bug

A test that has never failed proves nothing. `test_limit_is_never_exceeded_under_load`
was checked against a deliberately naive read-modify-write implementation, which
granted 200 requests against a limit of 50 and left the stored counter at 21.

This check is automated. The `mutation` job in CI runs
`.github/scripts/break_atomicity.py`, which swaps the atomic statement for the
naive version, and then **fails the build if the tests still pass**. So the
suite is verified to be capable of failing on every push, not just assumed to be.

If you change anything in `store.py`, that job is your safety net. The
two-request test is the readable illustration; the high-concurrency ones are
the real detectors.

## CI

`.github/workflows/tests.yml` runs the full suite against a real Postgres 16 on
Python 3.10, 3.11, 3.12 and 3.13, then runs the mutation job.
