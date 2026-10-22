# Instagram → YouTube Shorts Automation

Paste an Instagram reel/video URL into a Google Sheet → the bot downloads the clean
(watermark-free) video, converts it to a strict YouTube Short, uploads it with
auto-generated title/description/tags/hashtags, schedules it on an hourly ladder,
and moves the URL to an **Uploaded** tab. It repeats until the queue is empty and
picks up new URLs on its next run — fully unattended on GitHub Actions.

```
                 ┌──────────────────────────────────────────────────────────────┐
  you paste URL  │                    GitHub Actions (hourly)                   │
  ──────────────▶│                                                              │
   [Pending]     │  Sheets ─▶ yt-dlp ─▶ ffmpeg ─▶ metadata ─▶ YouTube API      │
   [Uploaded] ◀──│  queue     download   9:16        title/     private +      │
   [Failed]      │  lock      (no wm)    H.264/AAC  tags      publishAt        │
   [Log]    ◀────│                                              ↓               │
                 └──────────────────────────────────────────│───────────────────┘
                                                            ▼
                                              public at 16:15, 17:15, 18:15 …
```

---

## What it guarantees

| Requirement | How it is enforced |
|---|---|
| No Instagram watermark | yt-dlp pulls the clean stream the IG web player uses (not the in-app "Save" copy that burns in the logo) |
| Strict Shorts format | `ffmpeg` re-encodes to **1080×1920**, 9:16, H.264 + AAC, 30 fps, `+faststart`, duration ≤ **180 s**, silent audio track added if missing; output is ffprobe-verified before upload |
| Correct metadata | Title ≤ 100 chars + `#shorts`; description = cleaned caption + credit + hashtag block; tags ≤ 500 chars, deduped |
| One Short per hour | Each upload is `private` with a `publishAt` = **max(now + 15 min, last publish + 60 min)** — YouTube publishes automatically |
| Nothing is lost | Row only leaves *Pending* after YouTube returns a video id; failures are retried on later runs, then moved to *Failed* after `max_attempts` |
| No double uploads | Spreadsheet lock + GitHub `concurrency` group; a crashed run's stuck rows are auto-reset |

---

## 1. Prerequisites

* A GitHub account (free tier is enough) — the bot runs in Actions.
* A Google account that owns the YouTube channel (**2-step verification is not required, but the channel must be able to upload**).
* That's it — ffmpeg/Python are installed by the workflow.

> ⚠️ **Use this only for videos you own or have permission to repost.** Re-uploading
> other people's reels risks copyright strikes, Content ID claims and channel
> termination. The bot cannot judge rights for you.

---

## 2. Get the code into GitHub

1. Create a new **private** repository (e.g. `insta-shorts-bot`).
2. Upload this folder (or `git push` it). Keep the structure — the workflow lives
   at `.github/workflows/shorts.yml`.

---

## 3. Google Cloud setup (part 1: the Sheet writer)

Full click-by-click version: **[docs/SETUP_GOOGLE.md](docs/SETUP_GOOGLE.md)** — here's the short form.

1. <https://console.cloud.google.com> → create project `shorts-bot`.
2. **APIs & Services → Library** → enable **Google Sheets API** and **YouTube Data API v3**.
3. **APIs & Services → Credentials → Create credentials → Service account**
   → name it `sheet-writer` → Create → skip roles → Done.
4. Open it → **Keys → Add key → JSON** → download → rename to `service_account.json`.
   It contains the `client_email` — you'll share the Sheet with that address.
5. **Credentials → Create credentials → OAuth client ID → Desktop app**
   → download → save as `client_secret.json`.
6. **OAuth consent screen**: User type *External*, add your own Gmail under
   **Test users**, then **Publish app** (see the 7-day token warning in the doc).

---

## 4. Get your YouTube refresh token (once, on your computer)

A browser login can't happen inside GitHub Actions, so you do it once locally:

```bash
pip install -r requirements.txt
python auth_setup.py          # opens your browser; approve with the channel's Google account
```

It prints three values and saves `youtube_secrets.json`. Add them (plus the others
below) under **Repo → Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `YT_CLIENT_ID` | from `youtube_secrets.json` |
| `YT_CLIENT_SECRET` | from `youtube_secrets.json` |
| `YT_REFRESH_TOKEN` | from `youtube_secrets.json` |
| `GCP_SA_JSON` | paste the **entire** `service_account.json` file contents |
| `SHEET_ID` | the id from your Sheet URL `.../spreadsheets/d/`**`THIS_PART`**`/edit` |
| *(optional)* `IG_COOKIES_B64` | only if you ever need private/age-gated posts |
| *(optional)* `HTTP_PROXY_URL` | residential proxy if Instagram rate-limits GitHub's IPs |

Never commit `service_account.json`, `client_secret.json` or `youtube_secrets.json` —
`.gitignore` already excludes them.

---

## 5. Create the queue sheet

1. Create a blank Google Sheet, e.g. **Shorts Queue**.
2. Share it with the service-account email (`client_email` from the JSON) as **Editor**.
3. Copy its id into `SHEET_ID` (or `config.yaml → google.sheet_id`).
4. Create the tabs automatically:

```bash
SHEET_ID=xxxxxxxx python tools/setup_sheet.py --show-email   # prints the email to share with
SHEET_ID=xxxxxxxx python tools/setup_sheet.py                # creates Pending/Uploaded/Failed/Log/State
```

Tabs and columns:

| Tab | Columns | Notes |
|---|---|---|
| **Pending** | `URL, Status, Attempts, Added At (UTC), Last Error` | you only paste into **URL**; leave the rest alone |
| **Uploaded** | `Instagram URL, YouTube URL, Video ID, Title, Publish At (UTC), Uploaded At (UTC), Duration (s), IG Author` | rows are *moved* here |
| **Failed** | `Instagram URL, Error, Attempts, Moved At (UTC)` | after `max_attempts` |
| **Log** | `Timestamp, Level, Message` | audit trail of every run |
| **State** | `Key, Value` | run lock (do not edit) |

---

## 6. Turn it on

* **Manually:** Actions → *Instagram to YouTube Shorts* → **Run workflow**
  (optionally `dry_run = true` to test select → convert → metadata without uploading).
* **Automatically:** the cron `5 * * * *` runs it every hour at :05 UTC.

Each run: acquires the lock → checks today's quota → takes the oldest pending URL →
downloads → converts → uploads as private with the next free hourly slot → moves the
row → exits. Hourly runs mean your queue drains one Short per hour, forever, with new
URLs you add being picked up automatically.

### Proven locally

```bash
python main.py --dry-run --url https://www.instagram.com/reel/XXXXXXXXXXX/   # no upload, no credentials needed
python main.py --check        # verifies ffmpeg, Sheet access, YouTube auth
python -m pytest tests -q     # 32 tests: format, metadata, ladder, queue logic
```

---

## The publish ladder, exactly

`publishAt = max(now + publish_buffer_minutes, last_publish_at + min_gap_minutes)`
where `last_publish_at` is the newest value in the **Uploaded** tab.

| Event | Result |
|---|---|
| You add a URL at 16:00, run at 16:05 | uploaded immediately, `publishAt` ≈ **16:20** |
| Next URL processed at 16:10 | `publishAt` = 16:20 + 60 = **17:20** (not 16:25) |
| Next URL processed three hours later | `publishAt` = max(now+15, last+60) → **the later one** — never in the past |
| Upload is slow and finishes at 17:00 anyway | still **17:20** for the previous one, so the gap holds |

Change the knobs in `config.yaml`:

```yaml
youtube:
  min_gap_minutes: 60          # the one-hour rule
  publish_buffer_minutes: 15   # first slot offset after upload
  daily_upload_limit: 6        # API-quota safety cap
  uploads_per_run: 1           # videos per workflow run
  timezone: Asia/Kolkata       # used for "today" accounting
```

---

## Quota maths (why 6 per day by default)

| Call | Units |
|---|---|
| `videos.insert` (upload) | **1600** |
| `playlistItems.insert` (optional) | 50 |
| Daily quota (default) | **10 000** |

→ 10 000 / 1600 = **≈ 6 uploads/day** per Google Cloud project. When quota runs out the
bot stops cleanly, leaves every row in place, and resumes on the next run. Need more?
See **[docs/QUOTA_AND_SCHEDULING.md](docs/QUOTA_AND_SCHEDULING.md)** (quota-increase form,
or a second Cloud project with its own OAuth token).

---

## CLI reference

| Command | Purpose |
|---|---|
| `python main.py` | process the sheet with `config.yaml` defaults |
| `python main.py --limit 6` | up to 6 uploads this run (one per hourly slot) |
| `python main.py --dry-run` | download + convert + print metadata, no upload, no sheet writes |
| `python main.py --url <URL>` | test one URL end-to-end, bypassing the sheet |
| `python main.py --add-url <URL>` | append a URL to Pending |
| `python tools/queue_urls.py links.txt` | bulk-add a file of URLs |
| `python main.py --check` | health check: ffmpeg, Sheet, YouTube auth |
| `./run_local.sh --dry-run` | same as above but loads `.env` first (copy `.env.example`) |

---

## Config reference (`config.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `download.cookies_file` | `""` | optional `cookies.txt` for private/age-gated posts |
| `download.proxy` | `""` | proxy for Instagram requests |
| `download.max_duration_sec` | `180` | longer videos are rejected (or trimmed, see next) |
| `download.trim_longer_videos` | `false` | `true` = cut to 180 s instead of failing |
| `video.width/height` | `1080/1920` | output resolution (keep 9:16) |
| `video.pad_mode` | `blur` | `blur` bars, `black` bars, or `crop` |
| `video.crf` / `preset` | `20` / `veryfast` | quality vs. file size/speed |
| `video.ensure_audio` | `true` | adds silence if the source is mute (YouTube needs audio) |
| `youtube.category_id` | `24` | 24 Entertainment, 22 People & Blogs, 23 Comedy, 10 Music |
| `youtube.playlist_id` | `""` | optionally add every Short to a playlist |
| `youtube.notify_subscribers` | `true` | ping subscribers on upload |
| `youtube.max_attempts` | `3` | failed attempts before a row goes to *Failed* |
| `metadata.title_suffix` | `#shorts` | keeps the Short signal in the title |
| `metadata.default_hashtags` | shorts, reels, … | always appended to the description |
| `metadata.credit_original_creator` | `true` | adds `Credit: @author` |
| `metadata.description_footer` | `""` | e.g. `Follow for more 🔥` |

Everything in `youtube.*` can be overridden by env vars in the workflow
(`DAILY_UPLOAD_LIMIT`, `UPLOADS_PER_RUN`, `MIN_GAP_MINUTES`, `TIMEZONE`,
`IG_COOKIES_FILE`, `HTTP_PROXY_URL`).

---

## Project layout

```
main.py                     CLI entry point
auth_setup.py               one-time local OAuth → refresh token
config.yaml                 all settings
requirements.txt            dependencies
src/
  config.py                 config + env overrides
  downloader.py             yt-dlp wrapper (clean IG stream, friendly errors)
  video.py                  ffprobe checks + strict Shorts transcode
  metadata.py               caption → title/description/tags/hashtags
  youtube.py                OAuth client, resumable upload, publish ladder
  sheets.py                 Sheet queue, lock, Uploaded/Failed/Log tabs
  pipeline.py               the orchestrator (runs until the queue is empty)
  logging_setup.py          console + run.log logging
tools/setup_sheet.py        create/verify the tabs
tools/queue_urls.py         bulk-add URLs
tests/                      32 unit + integration tests (no credentials needed)
docs/                       setup, troubleshooting, quota guides
```

---

## Known limits (read once)

* **6 uploads/day** on default quota — that's Google, not the bot.
* **Instagram fights scrapers.** Public reels usually work; if GitHub's runners get
  rate-limited you'll see `Instagram blocked the request` in the *Log* tab → add
  `HTTP_PROXY_URL` (residential proxy) or `IG_COOKIES_B64`.
* **A brand-new YouTube channel** may be limited to 15-minute uploads and can have
  uploads held for review; also don't upload the same file repeatedly (YouTube
  deduplicates).
* **GitHub pauses scheduled workflows after ~60 days of repo inactivity** — push any
  commit (or click "Run workflow") to re-enable. [Docs](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/disabling-and-enabling-a-workflow)
* Scheduled cron is **best effort** — GitHub can delay a run by a few minutes, which
  is why the ladder is computed from `last_publish_at`, not from the clock.
* Needs **2 GB+ free space** per video during processing (cleaned up right after).

---

## Troubleshooting

See **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** for every error message the
bot can produce and its fix (token expiry, quota, publishAt rejected, music/copyright
mutes, ffmpeg failures, sheet permission errors).
