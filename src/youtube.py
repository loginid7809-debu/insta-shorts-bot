"""YouTube Data API v3 upload (OAuth refresh-token flow, resumable upload)."""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import YouTubeConfig
from .metadata import VideoMetadata

SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
RETRYABLE_STATUS = {500, 502, 503, 504}
RETRYABLE_REASONS = {"rateLimitExceeded", "backendError", "internalError", "userRateLimitExceeded"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}


class YouTubeError(RuntimeError):
    pass


class QuotaExceeded(YouTubeError):
    """Daily API quota (or daily upload cap) is exhausted - stop, retry tomorrow."""


class UploadFailed(YouTubeError):
    pass


def build_client(logger=None):
    """Build an authenticated YouTube client from env credentials."""
    import os

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    client_id = os.environ.get("YT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YT_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and refresh_token):
        raise YouTubeError(
            "Missing YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN. "
            "Run `python auth_setup.py` locally once (see README step 4)."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    if logger:
        logger.info("Authenticated with YouTube API")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def format_publish_at(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _http_error_parts(exc) -> tuple[int, set[str], str]:
    """Best-effort extraction of status code, reasons and a readable message."""
    status = getattr(getattr(exc, "resp", None), "status", 0) or 0
    reasons: set[str] = set()
    message = str(exc)
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        err = payload.get("error", {})
        message = err.get("message", message)
        for item in err.get("errors", []) or []:
            reasons.add(str(item.get("reason", "")))
        if err.get("status"):
            reasons.add(str(err["status"]).lower())
    except Exception:
        pass
    return int(status), reasons, message


def upload_video(
    client,
    path: Path,
    meta: VideoMetadata,
    cfg: YouTubeConfig,
    publish_at: datetime | None,
    logger,
) -> str:
    """Upload *path* and return the new YouTube video id.

    If *publish_at* is given the video is uploaded as private with a scheduled
    publish time (YouTube then flips it public on its own).
    """
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    status_body: dict[str, object] = {
        "selfDeclaredMadeForKids": bool(cfg.made_for_kids),
        "license": "youtube",
    }
    if publish_at:
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = format_publish_at(publish_at)
    else:
        status_body["privacyStatus"] = "public"

    snippet: dict[str, object] = {
        "title": meta.title,
        "description": meta.description,
        "tags": meta.tags,
        "categoryId": cfg.category_id,
    }
    if cfg.default_language:
        snippet["defaultLanguage"] = cfg.default_language
        snippet["defaultAudioLanguage"] = cfg.default_language

    body = {"snippet": snippet, "status": status_body}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)

    logger.info(
        "Uploading to YouTube (%.1f MB) -> publish at %s",
        path.stat().st_size / 1_048_576,
        format_publish_at(publish_at) if publish_at else "immediately",
    )

    request = client.videos().insert(
        part="snippet,status", body=body, media_body=media,
        notifySubscribers=bool(cfg.notify_subscribers),
    )

    response = None
    attempt = 0
    while response is None:
        try:
            chunk, response = request.next_chunk()
            if chunk:
                logger.info("  upload progress: %d%%", int(chunk.progress() * 100))
        except HttpError as exc:
            code, reasons, message = _http_error_parts(exc)
            if reasons & QUOTA_REASONS:
                raise QuotaExceeded(message) from exc
            retryable = code in RETRYABLE_STATUS or code == 429 or bool(reasons & RETRYABLE_REASONS)
            attempt += 1
            if retryable and attempt <= 6:
                wait = min(2 ** attempt + random.random(), 60)
                logger.warning("Transient upload error (%s %s) - retry %s/6 in %.0fs", code, message[:120], attempt, wait)
                time.sleep(wait)
                continue
            raise UploadFailed(f"YouTube API error {code}: {message[:400]}") from exc
        except Exception as exc:  # network hiccups
            attempt += 1
            if attempt <= 6:
                wait = min(2 ** attempt, 30)
                logger.warning("Upload error (%s) - retry %s/6 in %.0fs", exc, attempt, wait)
                time.sleep(wait)
                continue
            raise UploadFailed(f"Upload failed: {exc}") from exc

    video_id = response.get("id", "")
    if not video_id:
        raise UploadFailed(f"YouTube returned no video id: {response}")
    logger.info("Uploaded video id %s", video_id)

    if cfg.playlist_id:
        try:
            client.playlistItems().insert(
                part="snippet",
                body={"snippet": {"playlistId": cfg.playlist_id,
                                  "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
            ).execute()
            logger.info("Added to playlist %s", cfg.playlist_id)
        except Exception as exc:  # playlist is a nice-to-have
            logger.warning("Could not add to playlist: %s", exc)
    return video_id


def next_publish_slot(
    now: datetime,
    last_publish: datetime | None,
    cfg: YouTubeConfig,
) -> datetime:
    """Next legal publish time: at least `buffer` from now AND >= last + gap.

    This is what turns "upload 6 videos now" into "one Short every hour".
    """
    earliest_now = now + timedelta(minutes=cfg.publish_buffer_minutes)
    if last_publish:
        earliest_after_last = last_publish + timedelta(minutes=cfg.min_gap_minutes)
        slot = max(earliest_now, earliest_after_last)
    else:
        slot = earliest_now
    return slot.replace(second=0, microsecond=0) + timedelta(
        minutes=1 if slot.second or slot.microsecond else 0
    )
