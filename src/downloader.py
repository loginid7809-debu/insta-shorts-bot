"""Instagram download via yt-dlp.

yt-dlp fetches the *clean* media stream that Instagram serves to its own web
player, which is the version without the "reels logo / username" watermark that
the in-app "Share > Save" flow burns in. No re-cropping or logo-blurring needed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DownloadConfig
from .errors import ShortsBotError, TransientError


class DownloadError(ShortsBotError):
    """Permanent: this URL will not work, retrying is pointless."""


class TransientDownloadError(DownloadError, TransientError):
    """Instagram blocked us / network hiccup: retry next hour, keep the row queued."""

    transient = True


class VideoTooLongError(DownloadError):
    """Permanent: the source video exceeds the Shorts duration limit."""


@dataclass
class DownloadedVideo:
    path: Path
    url: str
    video_id: str
    uploader: str          # @handle when known (used for the credit line)
    caption: str
    duration: float
    width: int
    height: int
    raw: dict[str, Any]
    uploader_name: str = ""  # display name

    @property
    def aspect(self) -> str:
        if self.height <= 0:
            return "?"
        ratio = self.width / self.height
        if abs(ratio - 9 / 16) < 0.03:
            return "9:16 (vertical)"
        if abs(ratio - 1.0) < 0.03:
            return "1:1 (square)"
        if ratio > 1:
            return "16:9 (horizontal)"
        return f"{self.width}x{self.height} (portrait {ratio:.2f})"


# (regex, friendly message, is_transient)
_ERROR_MAP = (
    (r"empty media response|no video formats found|unable to extract|"
     r"login required|rate.?limit|checkpoint|too many requests|429",
     "Instagram blocked the request (login/rate limit). Retried automatically next hour; "
     "add IG cookies (IG_COOKIES_B64) or a proxy (HTTP_PROXY_URL) for a permanent fix.",
     True),
    (r"timed out|timeout|connection|temporary failure|network|502|503|504|"
     r"read timed out|remote end closed",
     "Network problem while downloading from Instagram. Retried automatically next hour.",
     True),
    (r"private (account|video|post)|login required to view",
     "The post is from a private account. Public posts only.", False),
    (r"unsupported url",
     "This is not a supported Instagram post/reel URL.", False),
    (r"video is not available|404|not found|has been removed",
     "Post not found \u2014 deleted, or the URL is wrong.", False),
)


def classify_error(exc: Exception) -> tuple[str, bool]:
    """Return (human message, is_transient) for a yt-dlp failure."""
    raw = str(exc)
    first = raw.strip().splitlines()[0] if raw.strip() else exc.__class__.__name__
    short = re.split(r"\s+See\s+https?://", first)[0]
    short = re.sub(r"^ERROR:\s*", "", short).strip()
    for pattern, friendly, transient in _ERROR_MAP:
        if re.search(pattern, raw, re.IGNORECASE):
            return f"{friendly} [raw: {short[:220]}]", transient
    # unknown yt-dlp failure: assume it is worth retrying (safer than blacklisting)
    return short[:400], True


def build_ydl_opts(cfg: DownloadConfig, out_dir: Path) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "noplaylist": False,          # carousels: only the first item is taken
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "prefer_ffmpeg": True,
        "retries": cfg.retries,
        "fragment_retries": cfg.retries,
        "socket_timeout": cfg.socket_timeout,
        "consoletitle": False,
        "writethumbnail": False,
        "cachedir": False,
    }
    if cfg.cookies_file and Path(cfg.cookies_file).exists():
        opts["cookiefile"] = cfg.cookies_file
    if cfg.proxy:
        opts["proxy"] = cfg.proxy
    if cfg.extra_opts:
        opts.update(cfg.extra_opts)
    return opts


def _pick_file(info: dict[str, Any], out_dir: Path) -> Path:
    req = info.get("requested_downloads") or []
    for item in req:
        for key in ("filepath", "_filename"):
            if item.get(key) and Path(item[key]).exists():
                return Path(item[key])
    for key in ("filepath", "_filename"):
        if info.get(key) and Path(info[key]).exists():
            return Path(info[key])
    found = sorted(
        (p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() in
         {".mp4", ".mkv", ".webm", ".mov", ".m4v"}),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not found:
        raise DownloadError("Download finished but no video file was produced.")
    return found[0]


def download(url: str, out_dir: Path, cfg: DownloadConfig, logger: logging.Logger) -> DownloadedVideo:
    """Download an Instagram reel/post and return metadata about the file."""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError("yt-dlp is not installed (pip install -r requirements.txt)") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = build_ydl_opts(cfg, out_dir)

    logger.info("Downloading %s", url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("_type") in {"playlist", "multi_video"}:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    raise DownloadError("Instagram returned no video for this URL.")
                info = entries[0]
            path = _pick_file(info or {}, out_dir)
    except DownloadError:
        raise
    except Exception as exc:  # yt-dlp raises many exception types
        message, transient = classify_error(exc)
        raise (TransientDownloadError if transient else DownloadError)(message) from exc

    # Instagram often omits duration/dimensions -> fall back to ffprobe so the
    # 180 s Shorts guard is always enforced.
    duration = float(info.get("duration") or 0)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if not duration or not width or not height:
        try:
            from .video import probe
            meta = probe(path)
            duration = duration or meta.duration
            width, height = width or meta.width, height or meta.height
        except Exception as exc:  # pragma: no cover - ffprobe missing/broken
            logger.debug("Could not probe %s: %s", path, exc)

    if duration and duration > cfg.max_duration_sec:
        if not cfg.trim_longer_videos:
            raise VideoTooLongError(
                f"Source video is {duration:.0f}s but YouTube Shorts allow max "
                f"{cfg.max_duration_sec}s. Set download.trim_longer_videos: true to trim it."
            )
        logger.warning("Video is %.0fs \u2014 it will be trimmed to %ss", duration, cfg.max_duration_sec)

    return DownloadedVideo(
        path=path,
        url=url,
        video_id=str(info.get("id") or path.stem),
        uploader=_handle(info),
        uploader_name=str(info.get("uploader") or info.get("channel") or ""),
        caption=_caption(info),
        duration=duration,
        width=width,
        height=height,
        raw=info,
    )


def _handle(info: dict[str, Any]) -> str:
    """Best @username for the credit line (not the display name / numeric id)."""
    for key in ("channel", "creator", "uploader_id"):
        value = str(info.get(key) or "").strip()
        if value and not value.isdigit() and " " not in value:
            return value.lstrip("@")
    name = str(info.get("uploader") or "").strip()
    return name.lstrip("@")


def _caption(info: dict[str, Any]) -> str:
    """Prefer the real caption; never fall back to yt-dlp's 'Video by <user>' title."""
    description = str(info.get("description") or "").strip()
    if description:
        return description
    title = str(info.get("title") or "").strip()
    if title.lower().startswith(("video by", "photo by")):
        return ""
    return title
