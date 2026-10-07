-- Dashboard snapshots: the numbers shown in the Sheet, kept as JSON for a future portal.

CREATE TABLE IF NOT EXISTS dashboard_snapshot (
    id        INTEGER PRIMARY KEY,
    taken_at  TIMESTAMP NOT NULL,
    data      TEXT NOT NULL           -- JSON, see evo_tiktok/dashboard.py
);
CREATE INDEX IF NOT EXISTS dashboard_snapshot_taken ON dashboard_snapshot (taken_at);
