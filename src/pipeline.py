"""The orchestrator: pull URLs from the sheet, download, convert, upload, move.

Everything is idempotent and crash-safe:
  * a row is marked PROCESSING before work starts (stale rows are reset next run)
  * a row only leaves the Pending tab after YouTube confirmed the upload
  * quota exhaustion stops the run WITHOUT burning an attempt
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import downloader, video as video_mod
from .config import AppConfig
from .downloader import DownloadError, VideoTooLongError
from .metadata import VideoMetadata, build_metadata
from .sheets import PendingItem, SheetQueue, iso, utcnow
from .youtube import QuotaExceeded, UploadFailed, build_client, next_publish_slot, upload_video


@dataclass
class RunStats:
    uploaded: int = 0
    failed: int = 0
    requeued: int = 0
    stopped_reason: str = "queue empty"
    details: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"uploaded={self.uploaded} failed={self.failed} requeued={self.requeued} "
            f"stopped={self.stopped_reason}"
        )


class Pipeline:
    def __init__(self, cfg: AppConfig, sheets: SheetQueue, logger: logging.Logger, dry_run: bool = False):
        self.cfg = cfg
        self.sheets = sheets
        self.log = logger
        self.dry_run = dry_run
        self.tz = ZoneInfo(cfg.youtube.timezone)
        self._yt = None
        self.work_root = Path(cfg.work_dir)

    # ------------------------------------------------------------------ utils
    @property
    def yt(self):
        if self._yt is None:
            self._yt = build_client(self.log)
        return self._yt

    def _slot(self) -> datetime:
        now = utcnow()
        last = self.sheets.last_publish_at()
        slot = next_publish_slot(now, last, self.cfg.youtube)
        self.log.info("Next publish slot: %s IST (previous: %s)",
                      slot.astimezone(self.tz).strftime("%Y-%m-%d %H:%M"),
                      last.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if last else "none")
        return slot

    def _cleanup(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    # -------------------------------------------------------------------- run
    def run(self, limit: int | None = None, stop_after_quota_error: bool = True) -> RunStats:
        stats = RunStats()
        cfg = self.cfg

        if self.sheets.pending_count() == 0:
            self.log.info("Nothing in the Pending tab - exiting.")
            stats.stopped_reason = "queue empty"
            return stats

        self.sheets.reset_stale_processing()

        # ---- daily upload guard (protects the channel + API quota) ----
        todays = self.sheets.uploads_on(utcnow().astimezone(self.tz).date(), self.tz)
        limit_today = limit if limit is not None else cfg.youtube.uploads_per_run
        remaining_today = cfg.youtube.daily_upload_limit - todays
        self.log.info(
            "Today: %s/%s uploads used, %s allowed this run",
            todays, cfg.youtube.daily_upload_limit, min(limit_today, max(remaining_today, 0)),
        )
        if remaining_today <= 0:
            stats.stopped_reason = f"daily cap of {cfg.youtube.daily_upload_limit} reached"
            self.log.warning(stats.stopped_reason)
            return stats

        budget = min(limit_today, remaining_today)
        attempted: set[str] = set()   # each URL is tried at most once per run

        while budget > 0:
            pending = [p for p in self.sheets.list_pending() if p.url not in attempted]
            if not pending:
                stats.stopped_reason = "queue empty" if not self.sheets.list_pending() else "only failing rows left"
                break
            item = pending[0]
            attempted.add(item.url)
            self.log.info("=" * 70)
            self.log.info("Processing row %s: %s", item.row, item.url)

            try:
                self._process_one(item, stats)
                budget -= 1
                if budget <= 0:
                    stats.stopped_reason = "per-run limit reached"
            except QuotaExceeded as exc:
                self.log.error("YouTube quota exhausted: %s", exc)
                self.sheets.set_status_with_attempts(item.row, "PENDING", item.attempts,
                                                     "Quota exhausted - will retry after reset")
                self.sheets.append_log("QUOTA", f"Quota exhausted while processing {item.url}")
                stats.stopped_reason = "quota exhausted"
                if stop_after_quota_error:
                    break
            except (DownloadError, VideoTooLongError, UploadFailed) as exc:
                self._handle_failure(item, str(exc), stats)
            except Exception as exc:  # unexpected - log, retry later, keep draining
                self.log.exception("Unexpected error on %s", item.url)
                self._handle_failure(item, f"{exc.__class__.__name__}: {exc}", stats)

        self.log.info("Run finished: %s", stats.line())
        self.sheets.append_log("INFO", f"Run finished: {stats.line()}")
        return stats

    # --------------------------------------------------------------- one item
    def run_single(self, url: str) -> RunStats:
        """Process one URL without touching the Pending tab (manual test mode)."""
        from .sheets import PendingItem as _PI

        stats = RunStats()
        item = _PI(row=0, url=url, status="MANUAL", attempts=0, error="")
        try:
            self._process_one(item, stats)
        except QuotaExceeded as exc:
            self.log.error("Quota exhausted: %s", exc)
            stats.failed += 1
        except (DownloadError, VideoTooLongError, UploadFailed) as exc:
            self.log.error("Failed: %s", exc)
            stats.failed += 1
        except Exception as exc:
            self.log.exception("Unexpected error")
            stats.failed += 1
        return stats

    # --------------------------------------------------------------- one item
    def _process_one(self, item: PendingItem, stats: RunStats) -> None:
        cfg = self.cfg
        queued = item.row > 0
        if queued:
            self.sheets.set_status(item.row, "PROCESSING")

        tmp = Path(tempfile.mkdtemp(prefix="short_", dir=self._ensure_work_root()))
        try:
            # 1) download the clean, watermark-free Instagram stream
            dl = downloader.download(item.url, tmp, cfg.download, self.log)
            self.log.info("Downloaded %s (%s, %.1fs) from @%s",
                          dl.path.name, dl.aspect, dl.duration, dl.uploader or "unknown")

            # 2) metadata from the caption
            meta: VideoMetadata = build_metadata(dl.caption, cfg.metadata, uploader=dl.uploader)
            self.log.info("Title: %s", meta.title)
            self.log.info("Tags: %s", ", ".join(meta.tags[:15]))

            # 3) convert to strict Shorts format
            target = tmp / "shorts.mp4"
            info = video_mod.to_shorts(dl.path, target, cfg.video, self.log)
            if dl.path != target and dl.path.exists():
                dl.path.unlink(missing_ok=True)

            if self.dry_run:
                self.log.warning("[DRY RUN] would upload %s\n--- title ---\n%s\n--- description ---\n%s\n--- tags ---\n%s",
                                 info.summary(), meta.title, meta.description, ", ".join(meta.tags))
                if queued:
                    self.sheets.set_status(item.row, "PENDING")
                stats.details.append(f"dry-run: {item.url}")
                return

            # 4) upload + schedule
            slot = self._slot()
            video_id = upload_video(self.yt, target, meta, cfg.youtube, slot, self.log)
            watch_url = f"https://youtube.com/shorts/{video_id}"

            # 5) move the row (or just record it, in manual mode)
            record = [
                item.url, watch_url, video_id, meta.title,
                iso(slot), iso(utcnow()), f"{info.duration:.1f}", dl.uploader,
            ]
            if queued:
                self.sheets.move_to_uploaded(item.url, record)
            else:
                self.sheets.ws("uploaded").append_row(record, value_input_option="RAW")
            self.sheets.append_log(
                "UPLOADED",
                f"{item.url} -> {watch_url} | publish {slot.astimezone(self.tz):%Y-%m-%d %H:%M} IST",
            )
            stats.uploaded += 1
            stats.details.append(f"{watch_url} @ {slot.astimezone(self.tz):%H:%M}")
            self.log.info("SUCCESS: %s scheduled for %s IST",
                          watch_url, slot.astimezone(self.tz).strftime("%Y-%m-%d %H:%M"))
        finally:
            self._cleanup(tmp)

    def _handle_failure(self, item: PendingItem, error: str, stats: RunStats) -> None:
        attempts = item.attempts + 1
        max_attempts = max(1, self.cfg.youtube.max_attempts)
        self.log.error("Failed (%s/%s): %s", attempts, max_attempts, error)
        if attempts >= max_attempts:
            self.sheets.move_to_failed(item.url, error, attempts)
            self.sheets.append_log("FAILED", f"{item.url} :: {error[:300]}")
            stats.failed += 1
        else:
            self.sheets.set_status_with_attempts(item.row, "ERROR", attempts, error)
            self.sheets.append_log("RETRY", f"{item.url} attempt {attempts}: {error[:250]}")
            stats.requeued += 1

    def _ensure_work_root(self) -> Path:
        self.work_root.mkdir(parents=True, exist_ok=True)
        return self.work_root
