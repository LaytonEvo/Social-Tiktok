# Spike: can our TikTok business account read its own comments via API?

Checked: 1 October 2026 (desk research only; no sign-ups, no API calls).
Spec reference: `docs/SPEC.md` §8, open question 4.

**Method note:** the research sandbox could not open TikTok's doc sites directly (`business-api.tiktok.com`
and `developers.tiktok.com` were blocked by the network proxy). Claims marked "confirmed in official docs"
come from search-engine extracts of TikTok's own pages, with the page URL given. Someone with a browser
should click through each link before we build. Labels used: **[official]** = confirmed in official docs,
**[third-party]** = third-party source, **[unverified]** = not confirmed anywhere reliable.

## Short answer

**Yes, in principle, through the TikTok API for Business "Organic API" (Accounts API). It needs an approved
developer app, and approval is the main risk.** The consumer-facing APIs (Display, Content Posting, Research)
do not give us comment text.

## Which API does what

| API | Comment text on our own posts? | Notes |
|---|---|---|
| **API for Business: Organic API, Accounts API** | **Yes** | Lists comments on owned videos and replies to a comment; can also reply, hide/unhide, like and delete. [official] [Organic API overview](https://business-api.tiktok.com/portal/docs/organic-api-overview/v1.3), [About Organic API](https://business-api.tiktok.com/portal/docs/organic-api/v1.3) |
| API for Business: Ad Comments (`/open_api/v1.3/comment/list/`) | Ads only | Comments on ads (incl. Spark Ads), keyed by advertiser ID. Not organic posts. [official] [TikTok Business API SDK, CommentsApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/CommentsApi.md), [Reply to a comment](https://business-api.tiktok.com/portal/docs/reply-to-a-comment/v1.3) |
| API for Business: Mentions API | Comments that @mention us elsewhere | Not comments on our own posts. [official] [Mentions API reference](https://business-api.tiktok.com/portal/docs/mentions/v1.3) |
| Display API (developers.tiktok.com) | No | Scopes `user.info.basic`, `user.info.stats`, `video.list`; gives `comment_count` only. [official] [Display API get started](https://developers.tiktok.com/docs/en/display-api-get-started), [Scopes reference](https://developers.tiktok.com/docs/en/tiktok-api-scopes) |
| Content Posting API | No | Publishing only. Irrelevant: we never post. [official] [Content sharing guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines) |
| Research API | Yes (any public video), but **not for us** | Restricted to academic and not-for-profit researchers independent of commercial interests. [official] [Research API eligibility](https://developers.tiktok.com/products/research-api), [Query video comments](https://developers.tiktok.com/docs/en/research-api-specs-query-video-comments) |

**Sources disagree:** some third-party guides say "TikTok has no public API for reading comments"
([rapidevelopers](https://www.rapidevelopers.com/api-automations/how-to-automate-tiktok-comment-moderation-using-the-api),
[mallary.ai](https://mallary.ai/blog/tiktok-api)). That is true of developers.tiktok.com (Display, Content Posting)
but not of the separate API for Business, whose Organic API documents comment endpoints. Treat those guides as out of date
or narrow in scope.

## Endpoints and auth (Accounts API)

- Endpoints reported as `/business/comment/list/`, `/business/comment/reply/list/`, `/business/comment/hide/`,
  `/business/comment/delete/`, `/business/comment/like/` under base `https://business-api.tiktok.com/open_api/v1.3`.
  [third-party] (search extract of a Fixmation blog post, `fixmation.dev/blog/one-inbox-for-social-comments`; page not opened).
  Exact paths and parameters are **[unverified]** against the reference pages.
- Auth: OAuth access token authorised by the TikTok account owner, plus a `business_id` (the account's `open_id`
  from the token response). [official] [Accounts API authentication](https://business-api.tiktok.com/portal/docs/accounts-api-authentication/v1.3)
- Scope names: the permission group is called **"TikTok Accounts"**. Individual scope names such as `comment.list`
  or `comment.list.manage` appear in community material but I could not confirm them. **[unverified]**
  Related insight scopes `user.insights` and `video.insights` are mentioned by
  [postforme.dev](https://www.postforme.dev/resources/tiktok-vs-tiktok-business-api). [third-party]
- Fields returned (comment id, video id, text, username, create time, likes, reply count, owner, pinned, status).
  [third-party] [Saras Analytics schema](https://help.sarasanalytics.com/tiktok-business/schema-information)

## Access and approval

1. TikTok For Business account, then register as a developer and wait for the developer profile to be approved.
   [official] [Get started](https://business-api.tiktok.com/portal/docs/get-started-guide/v1.3), [Create a developer app](https://business-api.tiktok.com/portal/docs/create-a-developer-app/v1.3)
2. Create a developer app, describe its use, and pick only the permissions it needs. Review takes 2 to 3 business days. [official] (same page)
3. **Since 20 March 2026, any app requesting the "TikTok Accounts" permission must first submit the Accounts API Access
   Application Form.** [official] (same page, per search extract). This is an extra gate; approval criteria are not public. [unverified]
4. A company domain email is reportedly needed (a Gmail developer account is refused), with about 3 business days for the
   developer account plus 2 to 3 for the Accounts scope. [third-party] (Fixmation, as above)
5. The account itself must be a TikTok **Business** account (not personal/creator). [official, implied by "TikTok Accounts" being a
   business-account API] [About business accounts](https://ads.tiktok.com/help/article/tiktok-business-account?lang=en). Confirm in the form.
6. **UK eligibility:** no regional restriction found for the Accounts API. TikTok's region limits I found apply to the
   Research API and to regulated-goods posting, not to this. **[unverified]**

## Rate limits

API for Business uses per-app limit tiers (four levels) measured as QPS, QPM and QPD.
[official] [Rate limits overview](https://business-api.tiktok.com/portal/docs/rate-limits/v1.3). The figures for the comment
endpoints were not visible. **[unverified]** For one account, 7 videos a week and a daily poll, we expect to be far
below any limit: roughly (videos in last 30 days ≈ 30) × (pages of 20–50 comments) calls per day.

## Reply / hide via API (note only)

Reply, hide/unhide, like and delete exist in the Accounts API. [official] [Organic API overview](https://business-api.tiktok.com/portal/docs/organic-api-overview/v1.3).
**We will not use them.** Guardrail 1 stands: request read scopes only, so the token cannot post even by mistake.
If TikTok bundles read and manage into one scope, enforce no-write in code (no client method for write endpoints, plus a test).

## Third-party routes

- **Metricool Inbox** shows TikTok comments (business accounts only) and lets you reply in the app.
  [official, Metricool] [Inbox manager](https://help.metricool.com/inbox-manager-how-to-manage-messages-and-comments-from-metricool-s9zze),
  [TikTok comments](https://metricool.com/tiktok-comments/). Its API has a `GET /v2/inbox/post-comments?provider=TIKTOK` route
  [third-party] [metricool-cli source](https://github.com/Purple-Horizons/metricool-cli). Needs the Advanced plan (see `metricool.md`).
  Whether TikTok comments come back through that endpoint is **[unverified]**.
- **Windsor.ai TikTok Organic** connector: per-video metrics including comment *counts*. Comment *text* is not in its
  published field list. [official, Windsor] [TikTok Organic connector](https://windsor.ai/connectors/tiktok-organic/). **[unverified]** for text.
- Scrapers (Apify and similar) can read public comments without auth. Not recommended: against TikTok's terms and brittle.

## Recommendation

**Yes, but unproven for our account. Path: TikTok API for Business, Accounts API, read-only.** Fallback: Metricool
Inbox API if we take the Advanced plan anyway. Last resort: Karin copies new comments into the queue by hand (pilot week 1 already does this).

## Next steps for a developer

1. Create a TikTok For Business developer account with an `@evolutiongolf.co.uk` email (not Gmail). Note the date.
2. Fill in the Accounts API Access Application Form. Use case: "read comments on our own videos into an internal review
   queue; humans reply in the TikTok app; no automated posting". Request only the read scope(s).
3. Create the developer app, set the redirect URI, submit for review. Log the review time.
4. Once approved, Layton or Karin authorises the Evo TikTok business account via OAuth. Store tokens in env vars
   (`config/.env.example`), never in the repo. Plan for token refresh.
5. Spike script, `--dry-run` only: list our videos, then comments per video. Record real field names, paging, and the time window covered.
6. Hidden behind the same interface, build a Metricool fallback client in case approval stalls.

## Unverified until tried with the real account

- Exact scope names, and whether read can be requested without manage/reply.
- Whether the Access Application Form is approved for a small UK retailer, and how long it takes.
- Exact endpoint paths, parameters, page size, and whether replies-to-comments are included.
- Comment-endpoint rate limits; access-token lifetime and refresh behaviour.
- Whether comments on Spark Ads (boosted organic posts) appear in the organic list, the ad list, or both.
- Whether Metricool's inbox API returns TikTok comments.
