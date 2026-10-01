# Railway cron services

Railway project **evo-tiktok-ops** (Europe West): one service per job, all built from
this repo's `main` branch, sharing one Postgres database. Railway has retired
config-as-code files, so each service's start command, cron schedule and restart
policy (`NEVER`) are set on the service itself:

| Service | Start command | UK time | Railway cron (UTC) |
| --- | --- | --- | --- |
| stock | `python -m evo_tiktok.migrate && python -m evo_tiktok.stock` | 06:00 daily | `0 5,6 * * *` |
| scripts | `python -m evo_tiktok.migrate && python -m evo_tiktok.scripts` | 07:00 Monday | `0 6,7 * * 1` |
| replies | `python -m evo_tiktok.migrate && python -m evo_tiktok.replies` | 08:00 daily | `0 7,8 * * *` |
| report | `python -m evo_tiktok.migrate && python -m evo_tiktok.report` | 15:00 Friday | `0 14,15 * * 5` |

Railway cron runs in UTC and the UK moves between GMT and BST, so each service
fires at both candidate UTC hours. With `EVO_SCHEDULED=1` set on the service,
the job checks the UK hour against `schedule` in `config/settings.yaml` and
skips the run that doesn't match. Manual runs (without the flag) always run.

## Variables per service

- `EVO_SCHEDULED=1`
- `DATABASE_URL` as a reference to the Postgres service
- The secrets the job needs from `config/.env.example`. The stock job needs
  `SHOPIFY_STORE` and `SHOPIFY_ADMIN_TOKEN` (scopes `read_products` and
  `read_inventory` only). `MEMBER_PRICES_API_URL` and `MEMBER_PRICES_API_TOKEN`
  are added when Luke's members API is ready. The scripts job also needs
  `ANTHROPIC_API_KEY`. All jobs publish to the shared Google Doc, so they also need
  `GOOGLE_SERVICE_ACCOUNT_JSON` and `GOOGLE_DOC_ID` (see "Shared Google Doc" below).
- `config/settings.yaml` isn't committed. Until it is provided on the host, jobs
  fall back to `config/settings.example.yaml` and log a warning. To use a file
  elsewhere, set `EVO_SETTINGS` to its path.

## Reply queue sheet (replies job)

1. Create a Google Sheet with two tabs: `Inbox` and `Reply queue`.
2. `Inbox` row 1 headers: `Comment ID, Video link, Username, Comment, Date`
   (other common export headers such as `video_id` or `text` also work). Karin
   pastes new comments below; rows already drafted are skipped automatically.
3. Create a Google Cloud service account, enable the Sheets API, download its
   JSON key into `GOOGLE_SERVICE_ACCOUNT_JSON`, and share the sheet with the
   service account's email as Editor.
4. Set `outputs.reply_sheet_id` (from the sheet URL) and
   the sheet ID in `outputs.reply_sheet_id`.

Drafts are appended to `Reply queue`. Karin edits and posts each reply in the
TikTok app herself. After posting a video, link it to its script so replies can
check that video's stock:

```bash
python -m evo_tiktok.posts --week 2026-10-05 --script 3 --post <TikTok video URL>
```

## Weekly report (report job)

- Organic metrics: until Windsor or Metricool is connected, export per-video stats for the week
  (TikTok Studio or Metricool) and run the report by hand with `--organic-csv`. Once the TikTok
  Organic account is connected in Windsor, set `report.organic_source: windsor`, confirm the field
  names in `report.windsor.organic_fields`, and the Friday cron can run unattended.
- Paid: `report.paid_source: windsor` needs the TikTok Ads account connected in Windsor and
  `WINDSOR_API_KEY`. Use `none` or `--paid-csv` until then.
- Members: `members.source: manual` means passing `--new-members`, `--member-orders` and
  `--member-revenue`. `shopify` counts customers and orders matching the searches in settings.
- The report only recommends. `weekly_report.boost_status` stays "awaiting approval"; Layton
  approves (reply in the Doc or in person) and someone sets the spend in TikTok Ads Manager by hand.

## Shared Google Doc

1. Create a Google Doc, e.g. "Evo TikTok: weekly packs, replies and reports".
2. Share it with the service account's email (the `client_email` in its JSON key) as Editor.
3. Put the Doc ID (the long part of its URL between `/d/` and `/edit`) in `GOOGLE_DOC_ID`.

Each run adds its entry at the top, so the newest pack, reply summary or report is always first.
