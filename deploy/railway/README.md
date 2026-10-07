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
  `SHOPIFY_STORE`, `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` (see "Shopify app"
  below). `MEMBER_PRICES_API_URL` and `MEMBER_PRICES_API_TOKEN`
  are added when Luke's members API is ready. The scripts job also needs
  `ANTHROPIC_API_KEY`. All jobs publish to the shared Google Doc, so they also need
  the Google sign-in (`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
  `GOOGLE_OAUTH_REFRESH_TOKEN`) and `GOOGLE_DOC_ID` (see "Google sign-in" below).
- `config/settings.yaml` isn't committed. Until it is provided on the host, jobs
  fall back to `config/settings.example.yaml` and log a warning. To use a file
  elsewhere, set `EVO_SETTINGS` to its path.

## Reply queue sheet (replies job)

1. Create a Google Sheet with two tabs: `Inbox` and `Reply queue`.
2. `Inbox` row 1 headers: `Comment ID, Video link, Username, Comment, Date`
   (other common export headers such as `video_id` or `text` also work). Karin
   pastes new comments below; rows already drafted are skipped automatically.
3. Make sure the Google account used for the sign-in below can edit the sheet.
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
- Members: `members.source: members_api` (the default) reads new paid members, cancellations
  and the paid total from the members portal (`members.api_url`) and needs
  `MEMBERS_REPORTING_API_KEY` on the report service. Without the key, or if the portal is down,
  the report still goes out with a note. `manual` means passing `--new-members`,
  `--member-orders` and `--member-revenue`; `shopify` counts customers and orders matching the
  searches in settings.
- The report only recommends. `weekly_report.boost_status` stays "awaiting approval"; Layton
  approves (reply in the Doc or in person) and someone sets the spend in TikTok Ads Manager by hand.

## Google sign-in (Doc and reply sheet)

The jobs sign in to Google as a normal user (e.g. online@evolutiongolf.co.uk)
who clicks "Allow" once. This avoids service-account keys, which the
organisation blocks by default. Edits in the Doc show as made by that user.

1. **Turn on the APIs.** In console.cloud.google.com, with your project selected:
   APIs & Services → Library → enable **Google Docs API** and **Google Sheets API**.
2. **Consent screen.** Google Auth Platform (or APIs & Services → OAuth consent
   screen) → Get started. App name "Evo TikTok jobs", your support email,
   Audience **Internal**, contact email → Create. Internal keeps it to your
   Workspace and means the sign-in doesn't expire after 7 days.
3. **OAuth client.** Clients (or Credentials) → Create client → Application type
   **Web application**, name "Evo TikTok jobs". Under Authorised redirect URIs add
   `https://developers.google.com/oauthplayground` → Create. Copy the **Client ID**
   and **Client secret**.
4. **Get the refresh token.** Open https://developers.google.com/oauthplayground →
   gear icon (top right) → tick **Use your own OAuth credentials** → paste the
   Client ID and secret → Close. In the left panel, in "Input your own scopes", paste
   `https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/spreadsheets`
   → **Authorize APIs** → sign in as the account that owns the Doc → Allow.
   Then **Exchange authorization code for tokens** and copy the **Refresh token**.
5. **Railway.** Add `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
   `GOOGLE_OAUTH_REFRESH_TOKEN` and `GOOGLE_DOC_ID` as shared variables.

If the sign-in is ever revoked (the user is removed, or access is revoked in their
Google account settings), repeat step 4 and update the refresh token.

## Shared Google Doc

1. Create a Google Doc, e.g. "Evo TikTok: packs, replies and reports", owned by or
   shared (Editor) with the account used for the sign-in above.
2. Put the Doc ID (the long part of its URL between `/d/` and `/edit`) in `GOOGLE_DOC_ID`.

Each run adds its entry at the top, so the newest pack, reply summary or report is always first.

A service-account JSON key in `GOOGLE_SERVICE_ACCOUNT_JSON` also works, if your
organisation allows keys; share the Doc with its `client_email` instead.

## Shopify app

Shopify no longer lets you create apps in the admin ("Develop apps" is legacy).
Create one in the Dev Dashboard instead:

1. Shopify admin → Settings → Apps → **Develop apps** → **Build apps in Dev Dashboard**
   (or go straight to dev.shopify.com). Create an app, e.g. "Evo TikTok jobs".
2. In the app's version settings, set the access scopes to `read_products`,
   `read_inventory` and `read_locations` only (locations, so stock can be split by
   warehouse), then release the version.
3. Install the app on the evolutiongolf store.
4. App settings → copy the **Client ID** and **Client secret** into
   `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`. `SHOPIFY_STORE` is the store's
   `xxxx.myshopify.com` address.

Each run swaps the ID and secret for a 24-hour access token (client credentials
grant), so nothing needs refreshing by hand. The app and the store must be in the
same Shopify organisation. A legacy admin-created app's `shpat_` token in
`SHOPIFY_ADMIN_TOKEN` also works.
