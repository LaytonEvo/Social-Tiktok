-- AI video versions: each draft Layton reviews, what it was made from and what the checks found.
-- content_log gains the latest edit request from the Production tab and the version Layton approved.

CREATE TABLE IF NOT EXISTS video_version (
    id            INTEGER PRIMARY KEY,
    week_start    DATE NOT NULL,
    script_no     INTEGER NOT NULL,
    version       INTEGER NOT NULL,
    source        TEXT NOT NULL,      -- hash of the script the video was made from
    status        TEXT NOT NULL,      -- review / failed
    edit_request  TEXT,               -- the notes this version answers (empty for v1)
    plan          TEXT,               -- JSON: shots, voice, labels, headlines
    file_key      TEXT,
    preview_url   TEXT,
    checks        TEXT,               -- JSON: every check and its result
    flags         TEXT,               -- what Layton should look at
    cost_usd      NUMERIC(8, 2),
    created_at    TIMESTAMP NOT NULL,
    UNIQUE (week_start, script_no, version)
);

ALTER TABLE content_log ADD COLUMN edit_request TEXT;
ALTER TABLE content_log ADD COLUMN approved_version INTEGER;
