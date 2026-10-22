# Quota, scheduling and going beyond 6 uploads/day

## Why there is a hard limit

The YouTube Data API gives every Google Cloud **project** 10 000 units/day.
An upload (`videos.insert`) costs **1 600 units**. There is no way around it:

| Daily uploads | Units used | Fits default quota? |
|---|---|---|
| 1 | 1 600 | ✅ |
| 6 | 9 600 | ✅ (default cap) |
| 7 | 11 200 | ❌ |
| 20 | 32 000 | ❌ (needs ≈50 000 units) |

Resets at **midnight Pacific Time** = **12:30 PM IST** (13:30 during DST-free periods
Google uses PST/PDT, so 12:30 or 13:30 IST).

The bot handles this gracefully: when a run hits `quotaExceeded` it puts the row back
to `PENDING` **without burning an attempt**, logs it, and exits. Tomorrow's first run
picks up exactly where it stopped.

## How the hourly ladder interacts with the quota

You add 10 URLs at 16:00. Config: `daily_upload_limit: 6`, `min_gap_minutes: 60`.

| Run (hourly) | Action | Publishes at |
|---|---|---|
| 16:05 | upload #1 | 16:20 |
| 17:05 | upload #2 | 17:20 |
| 18:05 | upload #3 | 18:20 |
| 19:05 | upload #4 | 19:20 |
| 20:05 | upload #5 | 20:20 |
| 21:05 | upload #6 → daily cap reached | 21:20 |
| 22:05 → 12:05 | *"daily cap of 6 reached"* in the log | — |
| 12:30 (quota reset) → 13:05 | upload #7 | max(13:20, 21:20+60) = **13:20** |
| 14:05 | upload #8 | 14:20 |

So the whole queue drains with **exactly one hour between every publish**, and nothing
is ever scheduled in the past.

**Want faster?** Set `uploads_per_run: 6` and change the cron to `0 */6 * * *`
(4 runs/day). Each run uploads 6 videos whose publish times are still one hour apart
(they're all `private` until then, so no spam bursts).

## Option 1 — request a quota increase (free, takes days/weeks)

1. <https://support.google.com/youtube/contact/yt_api_form> (or
   *Cloud Console → IAM & Admin → Quotas → YouTube Data API v3 → Queries per day*
   → *Edit quota* → apply).
2. Answer honestly: **"Personal automation that uploads my own original Shorts to my
   own channel (~15/day). No third-party data, no redistribution of API data, no
   end users."**
3. Typical ask: **50 000 units/day** (≈31 uploads). Requests above that need a real
   business justification and an audit.
4. You get a form/invoice-like workflow, usually answered within 1–3 weeks.
   While waiting, the bot keeps working at 6/day.

After approval, raise `daily_upload_limit` in `config.yaml` / the workflow env
(`DAILY_UPLOAD_LIMIT`) to match — the bot never exceeds what you configure.

## Option 2 — a second Google Cloud project (instant, no approval)

Quota is per **project**, not per channel. So:

1. Create `shorts-bot-2` in Google Cloud, enable YouTube Data API v3.
2. Create another OAuth Desktop client, run `python auth_setup.py` with that
   `client_secret.json`, and get its own refresh token
   (same Google account, same channel — fine).
3. Add a second workflow copy (`shorts-2.yml`) using
   `YT_CLIENT_ID_2`, `YT_CLIENT_SECRET_2`, `YT_REFRESH_TOKEN_2` and the same Sheet.
4. Give it a different cron hour offset, e.g. `35 * * * *` and `DAILY_UPLOAD_LIMIT: 6`.

Result: up to 12/day, still one hour apart, because both workflows read the same
`last_publish_at` from the *Uploaded* tab and the *State* lock prevents collisions.
(If the lock is busy the second workflow just exits; the next run takes over.)

> Unverified OAuth apps are capped at ~100 users, which is irrelevant here (only your
> own account). Using multiple projects does not violate the API terms as long as each
> project's quota is respected.

## Option 3 — spread publishing instead of spreading uploads

Uploads (and therefore quota) and *publishing* are decoupled: a video is uploaded
`private` and YouTube publishes it at `publishAt`. So you could upload your 6/day at
any time and let the ladder publish them hours later — the gap logic
(`min_gap_minutes`) already does exactly that. Use a large
`publish_buffer_minutes` (e.g. 240) in `config.yaml` if you want uploads to happen in
a batch while publishing stays hourly.

## Cost

| Item | Cost |
|---|---|
| YouTube Data API | free (quota-bound) |
| Google Sheets API | free |
| GitHub Actions (public repo) | free |
| GitHub Actions (private repo, free plan) | 2 000 min/month — ~1 500 used by hourly runs at 2–3 min each. Move to `5 */2 * * *` to halve it. |
| Proxy (optional) | ~$3–10/month if Instagram blocks GitHub IPs |
