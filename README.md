# evo-tiktok-ops

The handover pack for building Evolution Golf's TikTok content jobs with Claude Code.

## Start here
1. Create a private repo and copy this folder into it.
2. Open it in Claude Code. `CLAUDE.md` loads automatically as the project brief.
3. First prompt to Claude Code:
   > Read CLAUDE.md and docs/SPEC.md. Propose a plan for milestone M0 (repo skeleton, config loading, Shopify bulk read with the exclusions and categoriser, validators with tests). List any questions from SPEC.md section 8 you need answered first.
4. Work milestone by milestone (SPEC.md section 1). Each milestone ends with its acceptance tests passing in `--dry-run`.

## Contents
- `CLAUDE.md`: standing brief and guardrails.
- `docs/SPEC.md`: architecture, data sources, jobs, tests and milestones.
- `docs/CONTENT_RULES.md`: voice, formats, price and brand rules, giveaway wording.
- `prompts/`: starter prompts for the three jobs.
- `config/`: example settings and environment variables.
- `data/evo_clearance_allocation.xlsx`: the 1 October 2026 stock allocation and routing rules to port.

## Development

```bash
pip install -e ".[dev]"
pytest
# Offline dry run against the test fixture (no Shopify, no database):
python -m evo_tiktok.stock --dry-run --from-jsonl tests/fixtures/shopify_bulk.jsonl
# Live: set SHOPIFY_STORE, SHOPIFY_ADMIN_TOKEN and DATABASE_URL, then
python -m evo_tiktok.migrate
python -m evo_tiktok.stock
```

`DATABASE_URL` accepts `postgresql://...` (Railway) or `sqlite:///path.db` (local).
Every run appends to `logs/runs.jsonl`; live runs also write a `run_log` row.

## Layout (M0)

- `evo_tiktok/config.py`: settings (`config/settings.yaml`, else the example) and secrets from the environment.
- `evo_tiktok/shopify.py`: read-only bulk export of all variants, with exclusions, cost estimates and RRP checks.
- `evo_tiktok/allocation.py`: categoriser, age, size band, Members routing and the featured pool.
- `evo_tiktok/validators.py`: the guardrail checks from SPEC section 5.
- `evo_tiktok/member_prices.py`: placeholder for Luke's members portal API.
- `evo_tiktok/stock.py`: the stock snapshot job. `evo_tiktok/runner.py` handles shared CLI flags, the UK-hour guard and the run log.
- `migrations/`: database schema. `deploy/railway/`: cron service config.

## Script generator (M1)

```bash
# Dry run: real Claude call, pack written to out/packs/<week>/, nothing posted or logged
python -m evo_tiktok.scripts --dry-run --from-jsonl tests/fixtures/shopify_bulk.jsonl
```

- `evo_tiktok/scripts.py`: plans the mix (watch-time weighting after 2 weeks of metrics), picks Size Roulette
  sizes (3+ pairs, no repeat within 3 weeks) and the giveaway prize, calls Claude, validates every script and
  retries once with the errors before failing the run.
- `evo_tiktok/llm.py`: loads `prompts/*.md` and calls Claude with a JSON schema. The model comes from `models.scripts`.
- `evo_tiktok/pack.py`: the Markdown and PDF pack. Live runs add the pack to the shared Google Doc (`evo_tiktok/publish.py`).
- Live runs add 7 `content_log` rows with `status=planned`. Re-running the same week replaces them.

## Reply drafter (M2)

```bash
python -m evo_tiktok.replies --dry-run --from-csv tests/fixtures/comments.csv --from-jsonl tests/fixtures/shopify_bulk.jsonl
```

- Reads new comments from the `Inbox` tab of the reply sheet (or `--from-csv`) and skips any already queued.
- Spam and complaints are caught by fixed patterns as well as the model. Spam gets no draft and is marked `hide`.
  Complaints get the holding reply from settings, are always flagged, and are listed for Karin at the top of the day's Doc entry.
- Price questions get the fixed line from `docs/CONTENT_RULES.md`. Other drafts come from Claude
  (`models.replies`) using stock facts from the morning snapshot only, then pass the same guardrails plus a
  stock-consistency check. A draft that fails twice is left blank and flagged rather than failing the run.
- Output: `out/replies/*.csv` always; live runs add `reply_queue` rows, append to the `Reply queue` tab and post
  a summary to the shared Google Doc. There is no TikTok write code anywhere in the project.
- `python -m evo_tiktok.posts` links a posted video to its script (see `deploy/railway/README.md`).

## Weekly reporter (M3)

```bash
python -m evo_tiktok.report --dry-run --organic-csv week.csv --paid-csv ads.csv --new-members 12
```

- `evo_tiktok/metrics.py`: read-only sources: per-post organic metrics (CSV export or Windsor), TikTok Ads
  (Windsor or CSV) and members (manual or Shopify searches).
- `evo_tiktok/report.py`: ranks the week's linked posts and formats against the 4-week organic median, works out
  cost per result and per new member, tracks the pilot gate, and picks at most one boost candidate. A candidate must
  beat the median on both watch time and shares. The numbers table and the boost line are computed in code; Claude
  (`models.report`) writes the narrative and may not state a budget or ask for approval itself.
- Output: `out/reports/<date>/weekly_report.md` and `feed.json` (dashboard feed). Live runs save metrics to
  `content_log`, store the report in `weekly_report` with the boost "awaiting approval", and add the report to the shared Google
  Doc. No code changes ad spend.

## Where the output goes

All three jobs write to one shared Google Doc, newest entry at the top: Monday's filming pack, each day's reply
summary (complaints and drafts to check, with a link to the Reply queue sheet) and Friday's report with the boost
line. Set `GOOGLE_DOC_ID` (or `outputs.google_doc_id`) and the Google sign-in described in `deploy/railway/README.md`.
`outputs.destination: slack` switches back to Slack; `none` publishes nothing.
