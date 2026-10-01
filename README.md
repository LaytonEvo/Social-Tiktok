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
- `evo_tiktok/pack.py` and `evo_tiktok/slack.py`: the Markdown and PDF pack, and the Slack post (live runs only).
- Live runs add 7 `content_log` rows with `status=planned`. Re-running the same week replaces them.
