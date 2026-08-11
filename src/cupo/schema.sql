-- Cupo v0.1 schema
--
-- Two tables. That is the whole storage layer.
--
--   cupo_counters : one row per (customer, feature, window). The only mutable state.
--   cupo_events   : the idempotency ledger. One row per accepted track() call.
--
-- Everything is keyed on window_start rather than "reset by cron", so a window
-- rollover needs no scheduled job: the new window simply has no row yet.

CREATE TABLE IF NOT EXISTS cupo_counters (
    customer_id  TEXT        NOT NULL,
    feature      TEXT        NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    used         BIGINT      NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, feature, window_start),
    CONSTRAINT cupo_counters_used_non_negative CHECK (used >= 0)
);

CREATE TABLE IF NOT EXISTS cupo_events (
    idempotency_key TEXT        PRIMARY KEY,
    customer_id     TEXT        NOT NULL,
    feature         TEXT        NOT NULL,
    units           BIGINT      NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Used by usage() to read a customer's whole plan state in one round trip.
CREATE INDEX IF NOT EXISTS cupo_counters_customer_window
    ON cupo_counters (customer_id, window_start);

-- Used when pruning the ledger; events older than the retention window are
-- safe to drop once their window has closed.
CREATE INDEX IF NOT EXISTS cupo_events_created_at
    ON cupo_events (created_at);
