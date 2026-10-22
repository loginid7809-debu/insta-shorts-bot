"""Configuration loading for the Instagram -> YouTube Shorts bot.

Values come from (highest priority first):
  1. Environment variables (used by GitHub Actions / secrets)
  2. config.yaml
  3. Built-in defaults
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class GoogleConfig:
    sheet_id: str = ""
    service_account_file: str = "service_account.json"
    pending_tab: str = "Pending"
    uploaded_tab: str = "Uploaded"
    failed_tab: str = "Failed"
    log_tab: str = "Log"
    state_tab: str = "State"


@dataclass
class DownloadConfig:
    cookies_file: str = ""
    proxy: str = ""
    max_duration_sec: int = 180
    trim_longer_videos: bool = False
    retries: int = 4
    socket_timeout: int = 30
    extra_opts: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    max_duration_sec: int = 180
    pad_mode: str = "blur"  # blur | black | crop
    crf: int = 20
    preset: str = "veryfast"
    ensure_audio: bool = True


@dataclass
class YouTubeConfig:
    category_id: str = "24"
    made_for_kids: bool = False
    default_language: str = "en"
    playlist_id: str = ""
    notify_subscribers: bool = True
    publish_buffer_minutes: int = 15
    min_gap_minutes: int = 60
    daily_upload_limit: int = 6
    uploads_per_run: int = 1
    max_attempts: int = 3
    timezone: str = "Asia/Kolkata"


@dataclass
class MetadataConfig:
    title_suffix: str = "#shorts"
    title_max_len: int = 100
    fallback_title: str = "Wait for it \U0001F631"
    default_hashtags: list[str] = field(
        default_factory=lambda: ["shorts", "reels", "viral", "trending", "foryou"]
    )
    extra_tags: list[str] = field(default_factory=list)
    max_hashtags_in_description: int = 12
    description_footer: str = ""
    credit_original_creator: bool = True
    max_description_len: int = 4900


@dataclass
class AppConfig:
    google: GoogleConfig = field(default_factory=GoogleConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    work_dir: str = "work"

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "AppConfig":
        raw: dict[str, Any] = {}
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        cfg = cls(
            google=_build(GoogleConfig, raw.get("google", {})),
            download=_build(DownloadConfig, raw.get("download", {})),
            video=_build(VideoConfig, raw.get("video", {})),
            youtube=_build(YouTubeConfig, raw.get("youtube", {})),
            metadata=_build(MetadataConfig, raw.get("metadata", {})),
            work_dir=raw.get("work_dir", "work"),
        )

        # ---- environment overrides (GitHub Actions) ----
        env = os.environ
        if env.get("SHEET_ID"):
            cfg.google.sheet_id = env["SHEET_ID"].strip()
        if env.get("DAILY_UPLOAD_LIMIT"):
            cfg.youtube.daily_upload_limit = int(env["DAILY_UPLOAD_LIMIT"])
        if env.get("UPLOADS_PER_RUN"):
            cfg.youtube.uploads_per_run = int(env["UPLOADS_PER_RUN"])
        if env.get("MIN_GAP_MINUTES"):
            cfg.youtube.min_gap_minutes = int(env["MIN_GAP_MINUTES"])
        if env.get("TIMEZONE"):
            cfg.youtube.timezone = env["TIMEZONE"]
        if env.get("IG_COOKIES_FILE"):
            cfg.download.cookies_file = env["IG_COOKIES_FILE"]
        if env.get("HTTP_PROXY_URL"):
            cfg.download.proxy = env["HTTP_PROXY_URL"]
        return cfg


def _build(dc: type, data: dict[str, Any] | None):
    """Instantiate a dataclass from a dict, ignoring unknown keys."""
    if not data:
        return dc()
    allowed = {f.name for f in dc.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in allowed and v is not None}
    return dc(**kwargs)
