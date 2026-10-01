-- Initial schema. Portable SQL: runs on Postgres (Railway) and SQLite (local/tests).

CREATE TABLE IF NOT EXISTS run_log (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL UNIQUE,
    job           TEXT NOT NULL,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    dry_run       BOOLEAN NOT NULL,
    status        TEXT NOT NULL,          -- running / ok / skipped / failed
    summary       TEXT,                   -- JSON
    error         TEXT
);

CREATE TABLE IF NOT EXISTS stock_snapshot (
    id               INTEGER PRIMARY KEY,
    snapshot_id      TEXT NOT NULL,
    taken_at         TIMESTAMP NOT NULL,
    sku              TEXT NOT NULL,
    variant_id       TEXT NOT NULL,
    product_title    TEXT NOT NULL,
    variant_title    TEXT,
    vendor           TEXT,
    product_type     TEXT,
    status           TEXT,
    category         TEXT,
    season           TEXT,
    age              TEXT,
    size             TEXT,
    size_band        TEXT,
    junior           BOOLEAN,
    price            NUMERIC(10, 2),
    compare_at_price NUMERIC(10, 2),
    member_price     NUMERIC(10, 2),
    unit_cost        NUMERIC(10, 2),
    cost_estimated   BOOLEAN,
    units_warehouse  INTEGER,
    units_burley     INTEGER,
    units_total      INTEGER,
    members_units    INTEGER,
    featured         BOOLEAN,
    flags            TEXT,
    UNIQUE (snapshot_id, sku)
);
CREATE INDEX IF NOT EXISTS stock_snapshot_taken ON stock_snapshot (taken_at);

CREATE TABLE IF NOT EXISTS content_log (
    id                 INTEGER PRIMARY KEY,
    week_start         DATE NOT NULL,
    script_no          INTEGER NOT NULL,
    format             TEXT NOT NULL,
    hook               TEXT,
    on_screen_text     TEXT,
    caption            TEXT,
    hashtags           TEXT,
    featured_skus      TEXT,             -- JSON list
    roulette_size      TEXT,
    status             TEXT NOT NULL,    -- planned / filmed / posted / dropped
    tiktok_post_id     TEXT,
    posted_at          TIMESTAMP,
    views              INTEGER,
    avg_watch_seconds  NUMERIC(8, 2),
    completion_rate    NUMERIC(6, 4),
    shares             INTEGER,
    comments           INTEGER,
    follows            INTEGER,
    profile_visits     INTEGER,
    link_clicks        INTEGER,
    metrics_updated_at TIMESTAMP,
    created_at         TIMESTAMP NOT NULL,
    UNIQUE (week_start, script_no)
);

CREATE TABLE IF NOT EXISTS reply_queue (
    id                INTEGER PRIMARY KEY,
    comment_id        TEXT NOT NULL UNIQUE,
    post_id           TEXT,
    author            TEXT,
    comment_text      TEXT NOT NULL,
    category          TEXT,
    draft             TEXT,
    needs_human_check BOOLEAN NOT NULL,
    reason            TEXT,
    status            TEXT NOT NULL,     -- drafted / posted_by_person / hidden / skipped
    created_at        TIMESTAMP NOT NULL
);
