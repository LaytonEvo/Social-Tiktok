# Spike: Metricool for TikTok (API, plan, scheduling, analytics)

Checked: 1 October 2026 (desk research only; no sign-ups, no API calls).
Spec reference: `docs/SPEC.md` §8, open question 5.

**Method note:** the research sandbox could not open `metricool.com`, `help.metricool.com` or
`app.metricool.com` directly (blocked by the network proxy). Claims marked "official" come from search-engine
extracts of Metricool's own help pages, with the URL given. Click through before relying on them.
Labels: **[official]** = confirmed in official docs, **[third-party]** = third-party source, **[unverified]** = not confirmed.

## Short answers

| Question | Answer |
|---|---|
| Public REST API? | **Yes.** Token in the `X-Mc-Auth` header; docs online plus a Swagger file. [official] |
| Which plan? | **Advanced or Custom.** Not on Free or Starter, and no add-on unlocks it. [official + third-party] |
| Cost? | Advanced from about **US$53/month billed annually or US$67/month billed monthly** (15 brands). Roughly **£40–£50/month**, an estimate at ~£0.75 per US$. [third-party] |
| Schedule TikTok posts? | **Yes**, in the app and through the API. [official] |
| Auto-publish to TikTok? | **Yes, for business and personal accounts**, if the post fits TikTok's API limits. Otherwise it falls back to notification ("reminder") publishing with a final tap in the TikTok app. [official] |
| Per-post organic analytics via API? | **Mostly yes:** views, likes, comments, shares, reach, full-video-watched %, total and average watch time. Per-post follows and profile visits are **not confirmed**. [official + unverified] |

## API

- Metricool has a REST API with online docs at `https://app.metricool.com/resources/apidocs/index.html`, a downloadable
  Swagger file, and a PDF guide ([API English PDF](https://static.metricool.com/API+DOC/API+English.pdf), dated April 2024).
  [official] [API access help](https://help.metricool.com/api-access-export-your-metricool-data-to-other-tools-and-automate-tasks-x8ln5),
  [Nexla connector notes](https://docs.nexla.com/user-guides/connectors/metricool_api) [third-party]
- The token is in Account Settings → API. Requests carry the user ID and the brand ID (`blogId`). [third-party] [Tygart Media guide](https://tygartmedia.com/metricool-api-guide/)
- **Docs are incomplete:** Metricool itself says some endpoints are missing from the PDF and can be found by inspecting the browser.
  [official] [How to get an endpoint](https://help.metricool.com/en/article/how-to-get-an-endpoint-in-metricool-to-make-api-calls-15xciw7/).
  Expect some reverse-engineering.
- v2 routes seen in an open-source client: `/v2/scheduler/posts`, `/v2/analytics/posts/{network}`,
  `/v2/analytics/tiktok/profile`, `/v2/inbox/post-comments`. [third-party] [metricool-cli](https://github.com/Purple-Horizons/metricool-cli)
- Rate limits: not published anywhere I could find. **[unverified]**

## Plan and price

- API is "available only on Advanced and Custom plans". [official] [Plans, add-ons and API access](https://help.metricool.com/plans-add-ons-and-api-access-explained-xux1u)
- Advanced includes post approval, report templates, Looker Studio connector and the API; there is no separate API fee.
  [third-party] [SocialPilot, 2026](https://www.socialpilot.co/insights/metricool-pricing)
- Prices (USD; Metricool's site shows local currency, so GBP may differ):
  Starter US$20/month annual or US$25 monthly (5 brands); **Advanced US$53 annual or US$67 monthly (15 brands)**;
  US$85/US$107 (25 brands). [third-party] [SocialPilot](https://www.socialpilot.co/insights/metricool-pricing),
  [upload-post.com, "prices verified 17 September 2026"](https://www.upload-post.com/metricool-pricing/)
- **Sources disagree slightly:** [costbench](https://costbench.com/software/social-media-management/metricool/) lists Advanced at US$54.
  Check the live pricing page in GBP before buying. We need one brand, so we would pay for 15 brands to get the API.

## TikTok scheduling and publishing

- You can schedule and automatically publish TikTok videos for personal and business accounts, with the
  "Auto publish" toggle on. [official] [Schedule and post on TikTok](https://help.metricool.com/en/article/schedule-and-post-on-tiktok-pl1qpr/)
- Auto-publish limits: MP4/MOV, up to 500 MB, 3 seconds to 10 minutes, at least 540px; no line breaks in captions
  (a TikTok API limit). [official] [TikTok publishing troubleshooting](https://help.metricool.com/tiktok-publishing-and-upload-troubleshooting-guide-3ywdt)
- **Notification publishing** is needed for effects, stickers, filters or music outside TikTok's Top 100 trending sounds.
  Metricool then pushes a phone notification, saves the video to the gallery, copies the caption, and opens TikTok for
  the final tap. [official] [Manual publishing via notification](https://help.metricool.com/how-to-manually-publish-via-notification-5ds3m),
  [Set up notifications](https://help.metricool.com/how-to-set-up-notifications-for-manual-publishing-ybhgw)
- Whether the API can set auto-publish vs notification per post: **[unverified]** (the CLI above sends scheduler payloads,
  but I did not confirm the TikTok-specific fields).
- **Guardrail fit:** CLAUDE.md guardrail 1 forbids auto-posting by our jobs. Even with Metricool, our code must **not** call
  the scheduler's create-post endpoint. If the team wants Metricool scheduling, a person creates the post in the Metricool
  UI. The answer to SPEC question 5 is "yes, it can", but that is a choice for people, not a feature for the jobs.

## Organic TikTok analytics (per post)

Metricool's TikTok metrics page lists, per post: views (with source breakdown: For You, Following, Profile, Search,
Hashtag, Sound), likes, comments, shares, reach, duration, engagement, **full video views %**, **total and average watch time**.
At account level: profile views, followers. Business accounts get more data than personal ones.
[official] [TikTok metrics](https://help.metricool.com/en/article/tiktok-metrics-1hnbc9r/), [TikTok analytics guide](https://metricool.com/tiktok-analytics/)

| Metric we want | Per post in Metricool? |
|---|---|
| Views, comments, shares | Yes [official] |
| Average watch time | Yes [official] |
| Completion rate | Yes, as "full video views %" [official] |
| Follows per post | **Not listed per post** (followers at account level only) [unverified] |
| Profile visits per post | **Not listed per post** (profile views at account level only) [unverified] |

All of this is listed as UI metrics. That `/v2/analytics/posts/tiktok` returns every one of them is **[unverified]**.
Alternative: the Windsor.ai TikTok Organic connector lists "new followers per video" and "profile views per video"
along with watch time and full-watch rate. [official, Windsor] [TikTok Organic connector](https://windsor.ai/connectors/tiktok-organic/)

## Recommendation

**Use Metricool for analytics (and optionally the comment inbox) only if we pay for Advanced, about £40–£50/month.
Do not use its scheduling API from our jobs.** Metricool *can* auto-publish TikTok posts that fit the API limits,
but our guardrails keep publishing manual. For the Friday reporter, per-post views, watch time, completion, shares and
comments are covered. Follows and profile visits per post may need Windsor.ai or TikTok's own Accounts API
(`video.insights`, see `tiktok-comments.md`). If neither Metricool feature is needed beyond analytics, compare cost with Windsor.ai first.

## Next steps

1. Layton approves (or declines) the Advanced plan; check the live GBP price and the annual/monthly choice.
2. Start a trial if one includes API access, connect the Evo TikTok **business** account, copy the API token into env vars only.
3. Download the Swagger file; confirm the TikTok post-analytics fields, the inbox comments endpoint and the rate limits.
4. In `--dry-run`, call `/v2/analytics/posts/tiktok` for the last 7 days and map the fields to the weekly report.
5. Write a test that the Metricool client has no method that can create or publish a post (guardrail 1).

## Unverified until tried with the real account

- Live GBP price; whether a trial includes API access.
- Exact TikTok fields returned by the analytics API, especially follows and profile visits per post.
- API rate limits and data history depth (how far back post metrics go).
- Whether `/v2/inbox/post-comments` returns TikTok comments.
- Whether per-post auto-publish vs notification can be set through the API (only relevant if policy changes).
