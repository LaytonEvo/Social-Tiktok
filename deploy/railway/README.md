# Railway cron services

One Railway service per job, all from this repo, sharing one Postgres database.
Point each service's config-as-code path at its file here.

| Service | Config file | UK time | Railway cron (UTC) |
| --- | --- | --- | --- |
| stock | `deploy/railway/stock.json` | 06:00 daily | `0 5,6 * * *` |
| scripts | `deploy/railway/scripts.json` | 07:00 Monday | `0 6,7 * * 1` |
| replies (M2) | to add | 08:00 daily | `0 7,8 * * *` |
| report (M3) | to add | 15:00 Friday | `0 14,15 * * 5` |

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
  `ANTHROPIC_API_KEY` and `SLACK_BOT_TOKEN` (scopes `chat:write` and
  `files:write`; invite the bot to #evo-tiktok and set `outputs.slack_channel_id`
  so the PDF can be attached).
- `config/settings.yaml` isn't committed. Until it is provided on the host, jobs
  fall back to `config/settings.example.yaml` and log a warning. To use a file
  elsewhere, set `EVO_SETTINGS` to its path.
