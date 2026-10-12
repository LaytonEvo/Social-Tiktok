-- Production tracking: the outline each video is filmed from, when it's due and who owns it.
-- Status now runs planned -> filmed -> edited -> approved -> posted (or dropped),
-- set by the team in the Production tab of the dashboard Sheet.

ALTER TABLE content_log ADD COLUMN setup TEXT;
ALTER TABLE content_log ADD COLUMN shots TEXT;
ALTER TABLE content_log ADD COLUMN film_by DATE;
ALTER TABLE content_log ADD COLUMN post_on DATE;
ALTER TABLE content_log ADD COLUMN filmer TEXT;
ALTER TABLE content_log ADD COLUMN editor TEXT;
