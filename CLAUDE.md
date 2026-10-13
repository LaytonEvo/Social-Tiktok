# Evo TikTok Ops — Claude Code project context

Read this first, then `docs/SPEC.md`. This file is the standing brief for every session in this repo.

## What we're building

Three small scheduled jobs that support Evolution Golf's TikTok channel:

1. **Script generator** (weekly, Monday). Reads live stock from Shopify and writes a pack of 7 short-video scripts plus captions.
2. **Reply drafter** (daily). Turns new TikTok comments into draft replies in a review queue.
3. **Weekly reporter** (Friday). Pulls organic and paid stats and writes a one-page report with a boost recommendation.

The jobs prepare the work; Layton approves it. Videos can be AI-made (see `docs/VIDEO_STYLE.md`), but nothing is published without Layton's approval of that exact version.

## Business context

- Evolution Golf (GB Golf Online Ltd) is a UK golf e-tailer. Its core category is electric trolleys.
- TikTok drives sign-ups to **the Clubhouse** (Evo's membership, £36 a year) on evolutiongolf.co.uk. Clearance stock (aged apparel and footwear) sells there at member-only prices, behind a login.
- A three-week manual pilot runs in parallel with this build. Week 1 is manual; weeks 2–3 should run on these jobs as each one lands. Pilot results tune the prompts.
- Long-term content lane: **trolleys**. No UK competitor owns it on TikTok. Clearance content is the opening act.

## Non-negotiable guardrails (enforce in code, not just prompts)

1. **Nothing reaches TikTok without a human yes.** A video posts only after Layton marks that exact version Approved; a reply posts only after a person approves it. Complaints and flagged comments always stay with a person.
2. **No £ prices in videos** (the offer is what Clubhouse members save, as a %), and **no brand name next to a price or discount** in any caption, on-screen text or reply. Restricted brands are listed in `config/settings.example.yaml`. Validators fail the job if they find one.
3. **Price claims must be true.** Use "up to X% off" only if a meaningful share of featured lines is at X% (threshold in settings). Never show an RRP that Shopify doesn't hold.
4. **AI video only of real products, labelled.** AI footage is made from the product's own Shopify photos, never shows an AI version of a real person without their written consent, uses the agreed voice, and is posted with TikTok's AI-generated label. Every video passes the checks in `docs/VIDEO_STYLE.md` before Layton sees it.
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

- Layton: owner and sole approver: videos, spend, price claims and rule changes.
- Luke: co-owner.
- Karin: runs the weekly cycle and posts replies.
- Alex: films.

## When unsure

Ask in the PR rather than guess. Two integrations (the Metricool API and TikTok comment access) need a spike before building. See "Open questions" in SPEC.md.
