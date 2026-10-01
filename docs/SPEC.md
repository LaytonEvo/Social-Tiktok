# Evo TikTok Ops — build specification

Version 1, 1 October 2026. Owner: Layton. Builders: James / Joems with Claude Code.

## 1. Goal and timing

Have all three jobs running by the end of the three-week pilot, so a successful pilot scales immediately instead of waiting for a build.

| Week | Pilot | Build milestone |
| --- | --- | --- |
| 1 | Manual cycle in Claude chat | M0 repo, secrets, Shopify read; M1 script generator in dry-run |
| 2 | Scripts come from the generator | M1 live; M2 reply drafter; spikes on Metricool and TikTok comments done |
| 3 | Scripts and reply drafts from the jobs | M3 weekly reporter; M4 Metricool scheduling (if the spike passes) |
| End of 3 | Go/no-go | Hand over to Karin's weekly routine |

If the pilot fails its gate, stop after M3 and keep the jobs for low-effort organic posting.

## 2. Architecture

```
Shopify Admin API ──► Script generator (Mon) ──► Filming pack ──► Alex films ──► Metricool schedule
TikTok comments    ──► Reply drafter (daily) ──► Review queue ──► Karin edits and posts
Metricool + Windsor ─► Weekly reporter (Fri) ──► Report + boost recommendation ──► Layton approves
                                 ▲
                       content_log (shared store)
```

- **Runtime:** three cron jobs. Preferred host: Railway cron services (the team already uses Railway). Alternative: n8n Cloud calling HTTP endpoints. Pick one in M0.
- **Store:** one small database for `content_log`, `stock_snapshot`, `reply_queue` and `run_log`. Postgres on Railway or SQLite on a volume. Keep the schema in `migrations/`.
- **LLM:** Claude API. Model names live in settings, not code. Start with `claude-sonnet-5-5` for scripts and reports, and `claude-haiku-4-5-20251001` for reply drafts. Check model availability and pricing at docs.claude.com before going live.
- **Outputs:** Slack channel `#evo-tiktok` for packs and reports. The reply queue goes in a Google Sheet first and EvoTasks later. Confirm destinations in M0.

## 3. Data sources

### Shopify (store: evolutiongolf.co.uk)

- Read-only Admin GraphQL. For the full catalogue, use `bulkOperationRunQuery` rather than paging; there are 10,000+ variants.
- Fields per variant: `sku`, `title`, `price`, `compareAtPrice`, `selectedOptions`, `inventoryQuantity`, `inventoryItem.unitCost`, `inventoryItem.inventoryLevels` (locations: **Warehouse**, **Burley Golf Club**), `product { title vendor productType tags status }`.
- Known data issues, found 1 October 2026:
  - **Placeholder quantities.** Exclude "Item Personalization" (1,000,000,000 units) and any product type `PPLR_HIDDEN_PRODUCT`.
  - **Inconsistent product types** (e.g. "Golf Gloves" vs "Golf > Clothing > Golf Gloves > Mens"). Port the categoriser from the allocation logic (section 6).
  - **Missing costs.** Some variants have no unit cost. Estimate as `price / 1.88` and flag it.
  - **Missing RRPs.** Some lines have no `compareAtPrice`. These can't carry an RRP or discount claim.
  - **Season from tags.** Season comes from tags `SS25`, `AW25`, `SS26`, `AW26`, `SS27`, `Carryover`. Untagged lines are treated as aged.
- **Member prices:** source to be confirmed in M0, depending on how the members portal stores them (metafield, price list or a separate catalogue).

### TikTok comments

- **Pilot:** Karin pastes comments or exports them to a CSV in a watched folder or sheet.
- **Spike (M2):** check whether TikTok's official APIs give a business account read access to comments on its own posts. If yes, read them automatically. Replies stay manual either way.

### Metricool

- An MCP connector exists for Claude (tools include `createScheduledPost`, `getAnalyticsDataByMetrics`, `getBestTimeToPostByNetwork`).
- **Spike (M2):** confirm the Metricool REST API, plan tier needed, and whether TikTok posts can auto-publish or need a final tap in the app.

### TikTok Ads spend

- Already reachable through Windsor.ai (connected). The reporter reads spend, impressions and cost per result per post.

### Members

- Weekly count of new members and member orders. Source to be confirmed (Shopify customer tag or the members portal).

## 4. Jobs

### 4.1 Script generator — Monday 07:00 UK

**Input:** stock snapshot, last 4 weeks of `content_log` with metrics, `config/settings.yaml` (formats, featured lines, weekly mix).

**Logic:**

1. Build the featured pool: lines routed to "Members" in the allocation logic, with stock > 0 and an RRP present.
2. Pick this week's mix (default: 3 value comparisons, 2 Size Roulette, 1 giveaway, 1 trolley). Weight formats by the last 4 weeks' average watch time once 2+ weeks of data exist.
3. For Size Roulette, pick the size with the most units across featured footwear, without repeating a size within 3 weeks.
4. Call Claude with `prompts/script_generator.md` and a structured stock summary.
5. Validate the output (section 5). On failure, retry once with the validator errors appended, then fail loudly.

**Output:** a filming pack (markdown and PDF) with 7 scripts. Each script has hook, shot list, on-screen text, caption, hashtags, featured SKUs and a CTA. Write a row per script to `content_log` with `status=planned`.

**Acceptance tests:**

- 7 scripts, matching the configured mix.
- No restricted brand within 40 characters of a £ sign or "% off" in any caption or on-screen text.
- Every SKU named is in stock today.
- Size Roulette uses a size with 3+ pairs in stock.
- The giveaway script includes the free-entry wording.
- `--dry-run` makes no external writes.

### 4.2 Reply drafter — daily 08:00 UK

**Input:** new comments since last run (post ID, comment ID, text, author handle), the post's `content_log` row and the current stock snapshot.

**Logic:**

1. Classify each comment: question-sizing, question-stock, question-price, positive, complaint, spam, other.
2. Spam gets no draft and is marked for hiding. Complaints get a short holding reply and an alert to Karin.
3. Answer sizing and stock questions from the stock snapshot only, never from the model's memory.
4. Price questions point to the Members Club and never state a brand price.
5. Draft with `prompts/reply_drafter.md`, then validate.

**Output:** rows in the reply queue (sheet or EvoTasks) with comment, draft, category, and a flag where a person must check before posting.

**Acceptance tests:**

- No draft contains a brand name plus a price.
- Stock answers match the snapshot (seeded fixtures).
- Complaints are flagged, never auto-resolved.
- Nothing is posted to TikTok.

### 4.3 Weekly reporter — Friday 15:00 UK

**Input:** the week's posts from `content_log`; Metricool organic metrics (views, average watch time, completion, shares, comments, follows, profile visits, link clicks); Windsor TikTok Ads data; new members; member orders.

**Logic:** rank posts and formats, compare against the account median, calculate cost per member for paid, and pick one boost candidate. A candidate must beat the organic median on watch time and shares.

**Output:** a one-page report to Slack and a dashboard feed, ending with "Boost recommendation: [post], £[x]/day, [n] days — approve?". Update `content_log` with metrics.

**Acceptance tests:**

- Totals reconcile with source data on a seeded week.
- No boost is recommended for a post below the organic median.
- No spend is changed.

## 5. Validators (shared module)

- `brand_price_check(text)`: fails if a restricted brand and a price or discount appear within 40 characters.
- `up_to_claim_check(text, featured_lines)`: fails if "up to X%" appears and fewer than the configured share of featured lines are at X% or more.
- `rrp_check(sku)`: fails if a script shows an RRP for a SKU with no `compareAtPrice`.
- `giveaway_check(text)`: fails if a giveaway lacks the free-entry wording.
- `stock_check(skus)`: fails if any SKU has zero stock.

## 6. Reference: clearance allocation logic

`data/evo_clearance_allocation.xlsx` holds the 1 October 2026 allocation for 1,787 apparel and footwear variants. Port the categoriser and routing rules into `evo_tiktok/allocation.py` so the featured pool updates with live stock. The rules:

- **Categories:** Footwear, Gloves, Outerwear, Mid-layers, Shirts/Polos, Legwear, Headwear, Other apparel, Non-apparel. Categories come from the product type and title keywords.
- **Age:**
  - Live: AW26, SS27, Carryover.
  - Last season: SS26.
  - Aged: SS25, AW25 and untagged.
- **Core sizes:** footwear UK 8–10.5; apparel M–XL.
- **Members-routed lines (the TikTok featured pool):**
  - core-size footwear that isn't Live;
  - last-season shirts and legwear;
  - selected outerwear and mid-layers, split 60/40 with competition stock.

## 7. Configuration

See `config/settings.example.yaml` and `config/.env.example`. Secrets go in the host's secret store and are never committed.

## 8. Open questions (resolve in M0–M2)

1. ~~Railway cron or n8n?~~ **Decided: Railway cron** (see `deploy/railway/`).
2. Where do member prices live? **Luke is providing an API** for the members portal; `evo_tiktok/member_prices.py` is ready for it.
3. Reply queue: Google Sheet or EvoTasks?
4. Can a TikTok business account read its own comments via API? (spike)
5. Can Metricool auto-publish TikTok posts on our plan? (spike)
6. Which brands are restricted after the account decisions? (update settings)
7. Report destination: Slack, Evolution Golf Dashboard or morning briefing?

## 9. Out of scope

Auto-posting posts or replies, changing ad spend, AI video or voice, TikTok Shop listings, and DMs.
