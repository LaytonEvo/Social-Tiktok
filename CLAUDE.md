# Evo TikTok Ops — Claude Code project context

Read this first, then `docs/SPEC.md`. This file is the standing brief for every session in this repo.

## What we're building

Three small scheduled jobs that support Evolution Golf's TikTok channel:

1. **Script generator** (weekly, Monday). Reads live stock from Shopify and writes a filming pack of 7 short-video scripts plus captions.
2. **Reply drafter** (daily). Turns new TikTok comments into draft replies in a review queue.
3. **Weekly reporter** (Friday). Pulls organic and paid stats and writes a one-page report with a boost recommendation.

People film, post replies and approve spend. The jobs prepare the work; they never publish on their own.

## Business context

- Evolution Golf (GB Golf Online Ltd) is a UK golf e-tailer. Its core category is electric trolleys.
- TikTok drives sign-ups to the **Evo Members Club** on evolutiongolf.co.uk. Clearance stock (aged apparel and footwear) sells there at member-only prices, behind a login.
- A three-week manual pilot runs in parallel with this build. Week 1 is manual; weeks 2–3 should run on these jobs as each one lands. Pilot results tune the prompts.
- Long-term content lane: **trolleys**. No UK competitor owns it on TikTok. Clearance content is the opening act.

## Non-negotiable guardrails (enforce in code, not just prompts)

1. **Never auto-post anything to TikTok**: no posts, no comment replies. Output drafts only.
2. **No brand name next to a price** in any caption, on-screen text or reply. Restricted brands are listed in `config/settings.example.yaml`. Build a validator that fails the job if it finds one.
3. **Price claims must be true.** Use "up to X% off" only if a meaningful share of featured lines is at X% (threshold in settings). Never show an RRP that Shopify doesn't hold.
4. **Real footage and real voices only.** Scripts are for people to film. Never generate AI video or AI voiceover.
5. **Ad spend changes need a human yes.** The reporter recommends; it never changes spend.
6. **Giveaways always carry the free-entry wording** from `docs/CONTENT_RULES.md`.

## Conventions

- Python 3.12, one package `evo_tiktok/`, one CLI entry point per job: `python -m evo_tiktok.scripts`, `.replies`, `.report`.
- Config comes from environment variables (see `config/.env.example`) plus `config/settings.yaml`.
- Every job supports `--dry-run` (no external writes) and writes a run log.
- Prompts live in `prompts/` as markdown. Load them at runtime; don't hard-code them.
- Tests live in `tests/` and run with `pytest`. Each job has the acceptance tests listed in SPEC.md.
- British English in all generated content.

## People

- Layton: owner; approves spend and price claims.
- Luke: co-owner.
- Karin: runs the weekly cycle and posts replies.
- Alex: films.
- James (UK) and Joems (PH): CTOs; own this repo.

## When unsure

Ask in the PR rather than guess. Two integrations (the Metricool API and TikTok comment access) need a spike before building. See "Open questions" in SPEC.md.
