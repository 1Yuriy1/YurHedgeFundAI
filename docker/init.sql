-- YurHedgeFundAI — schema provisioned by docker compose (mounted into the
-- Postgres service's /docker-entrypoint-initdb.d/ so `docker compose up`
-- creates it on first boot). pgvector-ready: the extension loads here and the
-- signals table reserves an embedding column for Phase-3 semantic search.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS signals (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      TEXT        NOT NULL,
    run_date    DATE        NOT NULL,
    agent       TEXT        NOT NULL,
    tier        INTEGER     NOT NULL,
    ticker      TEXT,                -- NULL = market-wide signal (macro regime)
    ts          TIMESTAMPTZ NOT NULL,
    direction   TEXT        NOT NULL CHECK (direction IN ('bullish', 'neutral', 'bearish')),
    strength    DOUBLE PRECISION NOT NULL,
    confidence  DOUBLE PRECISION NOT NULL,
    horizon     TEXT        NOT NULL,
    red_flag    BOOLEAN     NOT NULL DEFAULT FALSE,
    thesis      TEXT        NOT NULL,
    evidence    JSONB       NOT NULL,
    risks       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    embedding   vector(1536)         -- reserved; nullable until evidence embeddings land
);

CREATE TABLE IF NOT EXISTS score_records (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      TEXT        NOT NULL,
    run_date    DATE        NOT NULL,
    ticker      TEXT,                -- NULL = market-wide group
    score       DOUBLE PRECISION NOT NULL,
    led_by      TEXT,
    red_flag    BOOLEAN     NOT NULL DEFAULT FALSE,
    votes       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_signals_run ON signals (run_id);
CREATE INDEX IF NOT EXISTS idx_signals_run_ticker ON signals (run_id, ticker);
CREATE INDEX IF NOT EXISTS idx_score_records_run ON score_records (run_id);
