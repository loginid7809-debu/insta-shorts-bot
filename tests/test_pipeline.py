"""Orchestration tests: queue draining, retries, quota stop, publish ladder.

Everything external (Instagram, ffmpeg, YouTube, Sheets) is replaced with a
fake so the *logic* is what gets tested - run it in seconds, no credentials.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import pipeline as P
from src.config import AppConfig
from src.downloader import DownloadError, TransientDownloadError, VideoTooLongError
from src.sheets import PendingItem
from src.video import VideoInfo
from src.youtube import QuotaExceeded, UploadFailed

LOG = logging.getLogger("test.pipeline")
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------- fakes
class FakeQueue:
    def __init__(self, urls, uploaded_today=0, last_publish=None):
        self.rows = [PendingItem(row=i + 2, url=u, status="PENDING", attempts=0, error="")
                     for i, u in enumerate(urls)]
        self.uploaded: list[list] = []
        self.failed: list[tuple] = []
        self.logs: list[tuple] = []
        self._uploaded_today = uploaded_today
        self._last_publish = last_publish
        self.status_writes: list[tuple] = []

    # -- what Pipeline uses --
    def pending_count(self):
        return len(self.rows)

    def reset_stale_processing(self):
        return 0

    def list_pending(self):
        return list(self.rows)

    def uploads_on(self, local_date, tz):
        return self._uploaded_today

    def last_publish_at(self):
        return self._last_publish

    def set_status(self, row, status, error=""):
        self.status_writes.append((row, status))

    def set_status_with_attempts(self, row, status, attempts, error=""):
        self.status_writes.append((row, status, attempts))
        for r in self.rows:
            if r.row == row:
                r.status, r.attempts, r.error = status, attempts, error

    def move_to_uploaded(self, url, record):
        from src.sheets import parse_ts
        self.uploaded.append(list(record))
        self.rows = [r for r in self.rows if r.url != url]
        self._last_publish = parse_ts(record[4])   # scheduled slot drives the ladder

    def move_to_failed(self, url, error, attempts):
        self.failed.append((url, error, attempts))
        self.rows = [r for r in self.rows if r.url != url]

    def append_log(self, level, message):
        self.logs.append((level, message))

    def ws(self, which):
        queue = self

        class _WS:
            def append_row(self, row, **_k):
                queue.uploaded.append(list(row))
        return _WS()


class FakeVideo:
    duration = 42.0


def make_cfg(tmp_path: Path, daily_limit: int = 100, min_gap: int = 60,
             buffer_min: int = 15, max_attempts: int = 2) -> AppConfig:
    cfg = AppConfig()
    cfg.work_dir = str(tmp_path / "work")
    cfg.youtube.publish_buffer_minutes = buffer_min
    cfg.youtube.min_gap_minutes = min_gap
    cfg.youtube.daily_upload_limit = daily_limit
    cfg.youtube.uploads_per_run = 1
    cfg.youtube.max_attempts = max_attempts
    return cfg


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Wire Pipeline to fakes; returns a configured Pipeline factory."""
    slots: list[datetime] = []

    def fake_download(url, out_dir, cfg, logger):
        class DL:
            path = tmp_path / "in.mp4"
            video_id = "ig123"
            uploader = "creator.one"
            caption = "Best dance move ever #dance #funny"
            duration = 30.0
            width, height = 1080, 1920
            aspect = "9:16 (vertical)"
        DL.path.write_bytes(b"fake")
        return DL()

    def fake_shorts(src, dst, cfg, logger=None):
        Path(dst).write_bytes(b"fake-shorts")
        return VideoInfo(width=1080, height=1920, duration=42.0, fps=30, has_audio=True,
                         vcodec="h264", acodec="aac")

    def fake_upload(client, path, meta, cfg, publish_at, logger):
        slots.append(publish_at)
        return f"vid{len(slots)}"

    monkeypatch.setattr(P.downloader, "download", fake_download)
    monkeypatch.setattr(P.video_mod, "to_shorts", fake_shorts)
    monkeypatch.setattr(P, "upload_video", fake_upload)
    monkeypatch.setattr(P.Pipeline, "yt", property(lambda self: object()))

    def factory(urls, daily_limit: int = 100, uploaded_today: int = 0,
                last_publish=None, max_attempts: int = 2):
        queue = FakeQueue(urls, uploaded_today=uploaded_today, last_publish=last_publish)
        cfg = make_cfg(tmp_path, daily_limit=daily_limit, max_attempts=max_attempts)
        pipe = P.Pipeline(cfg, queue, LOG)
        return pipe, queue, slots

    return factory


# --------------------------------------------------------------------- tests
def test_uploads_one_per_run_and_moves_the_row(env):
    pipe, queue, slots = env(["https://insta/p/1", "https://insta/p/2"])
    stats = pipe.run()

    assert stats.uploaded == 1
    assert len(queue.uploaded) == 1
    assert len(queue.rows) == 1                       # second URL still queued
    assert queue.uploaded[0][1].startswith("https://youtube.com/shorts/vid1")
    assert queue.uploaded[0][3].endswith("#shorts")   # title
    assert ("UPLOADED" in [lvl for lvl, _ in queue.logs])


def test_drains_whole_queue_when_limit_is_high(env):
    pipe, queue, slots = env(["u1", "u2", "u3"])
    stats = pipe.run(limit=10)

    assert stats.uploaded == 3
    assert queue.rows == []
    assert len(queue.uploaded) == 3


def test_publish_slots_are_one_hour_apart(env):
    pipe, queue, slots = env(["u1", "u2", "u3"])
    pipe.run(limit=3)

    assert len(slots) == 3
    gaps = {(b - a) for a, b in zip(slots, slots[1:])}
    assert gaps == {timedelta(hours=1)}


def test_daily_cap_stops_the_run(env):
    pipe, queue, slots = env(["u1"], uploaded_today=6, daily_limit=6)
    stats = pipe.run(limit=5)

    assert stats.uploaded == 0
    assert "daily cap" in stats.stopped_reason
    assert queue.rows[0].url == "u1"                  # untouched, will retry tomorrow


def test_permanent_failure_is_retried_then_moved_to_failed(env, monkeypatch):
    def boom(url, out_dir, cfg, logger):
        raise VideoTooLongError("Source video is 400s but YouTube Shorts allow max 180s")

    monkeypatch.setattr(P.downloader, "download", boom)
    pipe, queue, _ = env(["u1"])

    stats = pipe.run(limit=1)                          # attempt 1 -> requeue
    assert stats.requeued == 1 and stats.failed == 0
    assert queue.rows[0].attempts == 1
    assert len(queue.uploaded) == 0

    stats = pipe.run(limit=1)                          # attempt 2 -> failed tab
    assert stats.failed == 1
    assert queue.failed[0][0] == "u1"
    assert queue.rows == []


def test_quota_exhaustion_keeps_row_pending_and_stops(env, monkeypatch):
    def quota(*_a, **_k):
        raise QuotaExceeded("quotaExceeded")

    monkeypatch.setattr(P, "upload_video", quota)
    pipe, queue, _ = env(["u1", "u2"])
    stats = pipe.run(limit=5)

    assert stats.stopped_reason == "quota exhausted"
    assert len(queue.rows) == 2                       # nothing lost
    assert queue.rows[0].attempts == 0                # no attempt burned
    assert queue.failed == []


def test_empty_queue_is_a_noop(env):
    pipe, queue, slots = env([])
    stats = pipe.run()
    assert stats.uploaded == 0
    assert stats.stopped_reason == "queue empty"
    assert slots == []


def test_manual_url_bypasses_the_queue(env):
    pipe, queue, slots = env([])
    stats = pipe.run_single("https://insta/p/manual")

    assert stats.uploaded == 1
    assert len(queue.uploaded) == 1                   # recorded so the ladder continues
    assert queue.rows == []


# ------------------------------------------------- transient error handling
def test_transient_error_keeps_row_and_never_counts_an_attempt(env, monkeypatch):
    """An Instagram rate-limit / network blip must never send a good URL to Failed."""

    def blocked(url, out_dir, cfg, logger):
        raise TransientDownloadError("Instagram blocked the request (login/rate limit)")

    monkeypatch.setattr(P.downloader, "download", blocked)
    pipe, queue, _ = env(["u1"], max_attempts=1)   # even with max_attempts = 1

    for _ in range(5):                             # five hourly runs
        stats = pipe.run(limit=1)
        assert stats.failed == 0
        assert stats.requeued == 1

    assert len(queue.rows) == 1                    # still queued
    assert queue.rows[0].attempts == 0             # no attempt burned
    assert queue.rows[0].status == "PENDING"       # looks retryable, not broken
    assert queue.failed == []
    assert any(level == "RETRY-SOON" for level, _ in queue.logs)


def test_unexpected_network_error_is_treated_as_transient(env, monkeypatch):
    def flaky(url, out_dir, cfg, logger):
        raise RuntimeError("HTTPSConnectionPool: Read timed out. (read timeout=30)")

    monkeypatch.setattr(P.downloader, "download", flaky)
    pipe, queue, _ = env(["u1"], max_attempts=1)
    stats = pipe.run(limit=1)

    assert stats.failed == 0 and stats.requeued == 1
    assert queue.rows[0].attempts == 0


def test_permanent_error_still_counts_attempts(env, monkeypatch):
    def deleted(url, out_dir, cfg, logger):
        raise DownloadError("Post not found - deleted, or the URL is wrong.")

    monkeypatch.setattr(P.downloader, "download", deleted)
    pipe, queue, _ = env(["u1"], max_attempts=2)

    assert pipe.run(limit=1).requeued == 1
    assert queue.rows[0].attempts == 1
    assert pipe.run(limit=1).failed == 1
    assert queue.failed[0][0] == "u1"
