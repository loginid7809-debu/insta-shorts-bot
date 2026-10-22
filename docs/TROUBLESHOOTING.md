# Troubleshooting

Every message the bot writes into **Pending → Last Error**, **Failed → Error** or the
**Log** tab is listed here with its fix.

---

## Instagram / download errors

### `Instagram blocked the request (login/rate limit)…`
GitHub's shared runner IPs are often rate-limited by Instagram.
Fixes, in order of effort:
1. Just retry — the row stays pending and hourly runs retry it automatically.
2. Set a proxy: create `HTTP_PROXY_URL` secret (residential/mobile proxy) — the bot
   passes it to yt-dlp for all Instagram traffic.
3. Set cookies (also needed for private/age-gated posts):
   * On your computer, install a "Get cookies.txt LOCALLY" browser extension.
   * Log into instagram.com, export cookies for `instagram.com` → `cookies.txt`.
   * `base64 -w0 cookies.txt > cookies.b64` (macOS: `base64 -i cookies.txt -o cookies.b64`)
   * Put the contents into the `IG_COOKIES_B64` secret.
   Cookies expire every few weeks — expect to refresh them.

### `The post is from a private account. Public posts only.`
Use a public URL, or add cookies (above).

### `Post not found — deleted, or the URL is wrong.`
Reels must be `https://www.instagram.com/reel/<id>/` or `/p/<id>/`. Links to a
*profile* or *story* aren't videos and can't be downloaded.

### `Source video is 200s but YouTube Shorts allow max 180s`
Set `download.trim_longer_videos: true` in `config.yaml` (or the row fails by design).

### `Download finished but no video file was produced.`
Usually an image/carousel post with no video. Check the link in a browser.

---

## Video / ffmpeg errors

### `ffmpeg not found on PATH`
The workflow installs it (`apt-get install ffmpeg`). Locally: `sudo apt install ffmpeg`,
`brew install ffmpeg`, or `winget install ffmpeg`.

### `Output has no audio track`
Should be impossible with `video.ensure_audio: true`. If you set it to `false` and the
source is silent, YouTube can reject the upload — turn it back on.

### `ffmpeg failed: … Invalid data found when processing input`
The download was truncated (network). The row is retried next run; if it happens for
one specific reel every time, that reel is broken/DRM'd.

### The vertical video looks cropped or has black bars
`video.pad_mode` decides: `blur` (default, blurred background), `black` (bars),
`crop` (fills the frame, cuts the edges). A 1:1 or 16:9 source can never fill 9:16
without either bars or cropping — that's geometry, not a bug.

---

## YouTube API errors

### `quotaExceeded` / `dailyLimitExceeded`
The 10 000-unit daily quota is spent (≈6 uploads). The bot stops cleanly and leaves the
queue intact. It resets at **midnight Pacific Time** (≈12:30 IST). See
[QUOTA_AND_SCHEDULING.md](QUOTA_AND_SCHEDULING.md) to raise it.

### `uploadLimitExceeded`
Channel-level cap hit (new/unverified channels). Verify the channel at
<https://www.youtube.com/verify> and wait 24 h.

### Browser: `Access blocked … has not completed the Google verification process` / `Error 403: access_denied`
The OAuth app is in *Testing* mode and your Gmail is not an approved tester. Fix:
**OAuth consent screen → Audience → + ADD USERS → your Gmail → Save**, then re-run
`auth_setup.py` in an **incognito** window (the browser may otherwise use a different
Google account). Also confirm `client_secret.json` and the consent screen belong to the
**same** Cloud project. Full walkthrough:
[SETUP_GOOGLE.md § Access blocked](SETUP_GOOGLE.md#access-blocked-403-access_denied).

### `Invalid Credentials` / `invalid_grant` / `Token has been expired or revoked`
One of:
* the OAuth app is still in **Testing** → refresh tokens die after 7 days.
  Publish the app and re-run `auth_setup.py` (see SETUP_GOOGLE.md §8).
* you changed the Google account password or revoked access → re-run `auth_setup.py`
  and update the `YT_REFRESH_TOKEN` secret.

### `The request specifies publishAt … but the video is not private`
The API demands `privacyStatus=private` with `publishAt` — the bot always sets both.
If you edited the code, don't.

### `publishAt must be in the future` / `invalid publish time`
Your `publish_buffer_minutes` is too small (YouTube needs the time to be clearly in the
future). Keep it ≥ 15.

### `The user has exceeded the number of videos they may upload` / `videoTooLong`
A very new channel is limited to 15 minutes; irrelevant for Shorts, but wait a day.
Also: brand-new accounts sometimes get every upload held for review — check YouTube
Studio → Content.

### Upload works but the video is **silent / muted**
Your source contains music that Content ID matched. Nothing the bot can do — upload
your own audio or a different clip.

### Video uploads but doesn't appear as a Short
Shorts require **≤ 3 minutes AND vertical or square**. If the file is fine, give YouTube
a few minutes to process; the `https://youtube.com/shorts/<id>` URL works once it's done.
If you set `video.pad_mode: blur` on a horizontal source it still counts as vertical
(YouTube sees 1080×1920).

---

## Google Sheets errors

### `403 PERMISSION_DENIED` / `The caller does not have permission`
The sheet is not shared with the service account. Share it with
`client_email` from `service_account.json` as **Editor**.
Check with: `python tools/setup_sheet.py --show-email`

### `SpreadsheetNotFound`
`SHEET_ID` is wrong or you used the full URL. Only the id belongs in the secret.

### `WorksheetNotFound: Pending`
Run `python tools/setup_sheet.py` (or `python main.py --check`) once — the bot creates
missing tabs on its first real run too.

### `APIError: [429] Quota exceeded for Read requests`
You are hitting Google Sheets rate limits — usually because many runs overlap. The
workflow `concurrency` group plus the sheet lock already prevent this; if you run
locally at the same time as Actions, don't.

### Rows stuck on `PROCESSING`
A run was killed mid-video. The next run resets them automatically, or run
`python main.py --check` / wait an hour. The lock expires after 45 minutes.

---

## GitHub Actions issues

### The workflow doesn't run on schedule
* Scheduled workflows only run on the **default branch**.
* GitHub disables cron after ~60 days without repository activity — open the Actions
  tab and re-enable it, or push a commit.
* Private repos: Actions minutes are free for private repos on the free plan
  (2 000 min/month) — at ~2–3 min per hourly run that's ~1 500 min/month. If you're
  close to the limit, change the cron to `5 */2 * * *` (every 2 hours, two videos per
  run) or run it manually.

### `Input required and not supplied: sheet id` / auth errors in the log
A secret is missing or has a typo. Secret names are case-sensitive.

### Runs overlap and upload twice
`concurrency` in the workflow plus the lock in the *State* tab prevent it. If you
trigger manually while a scheduled run is in progress, GitHub will queue it, not
cancel it — so check the *Log* tab first.

### `The job running on runner … has exceeded the maximum execution time`
A very slow download. The timeout is 45 minutes; a normal Short takes 1–3 minutes.
Add `download.socket_timeout`/proxy, or raise `timeout-minutes`.

---

## Local testing without spending quota

```bash
python main.py --check                                  # credentials + ffmpeg + sheet
python main.py --dry-run --limit 2                      # download + convert only
python main.py --dry-run --url <instagram-url>          # one URL, full pipeline, no upload
python -m pytest tests -q                               # 32 tests, no network needed
```

`--dry-run` prints the exact title, description and tags that would be uploaded, so
you can tune `config.yaml → metadata` before the first real upload.
