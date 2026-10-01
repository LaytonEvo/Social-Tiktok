-- Weekly reports: the text sent, a dashboard feed and the boost awaiting approval.

CREATE TABLE IF NOT EXISTS weekly_report (
    id               INTEGER PRIMARY KEY,
    week_start       DATE NOT NULL,
    week_end         DATE NOT NULL UNIQUE,
    report_md        TEXT NOT NULL,
    feed             TEXT NOT NULL,          -- JSON
    gate             TEXT NOT NULL,          -- JSON
    boost_post_id    TEXT,
    boost_daily_gbp  NUMERIC(8, 2),
    boost_days       INTEGER,
    boost_status     TEXT NOT NULL,          -- none / awaiting approval (a person approves and sets spend)
    created_at       TIMESTAMP NOT NULL
);
