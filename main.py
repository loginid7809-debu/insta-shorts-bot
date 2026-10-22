"""CLI entry point.

Examples
--------
  python main.py                        # process the sheet with config defaults
  python main.py --limit 5              # upload at most 5 this run (1 per slot)
  python main.py --dry-run              # download + convert + print metadata, no upload
  python main.py --url https://...      # test ONE url, ignoring the sheet
  python main.py --add-url https://...  # queue a url into the Pending tab
  python main.py --check                # verify credentials, sheet access, ffmpeg
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from src.config import AppConfig
from src.logging_setup import setup_logging
from src.pipeline import Pipeline
from src.sheets import NullQueue, SheetQueue


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Instagram -> YouTube Shorts automation")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--limit", type=int, default=None, help="max uploads this run")
    p.add_argument("--dry-run", action="store_true", help="no upload, no sheet changes")
    p.add_argument("--url", default=None, help="process a single URL (sheet bypassed)")
    p.add_argument("--add-url", default=None, help="append a URL to the Pending tab and exit")
    p.add_argument("--check", action="store_true", help="health check then exit")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def health_check(cfg: AppConfig, sheets: SheetQueue, log: logging.Logger) -> int:
    ok = True
    log.info("work dir      : %s", Path(cfg.work_dir).resolve())
    for binary in ("ffmpeg", "ffprobe"):
        found = __import__("shutil").which(binary)
        log.info("%-14s: %s", binary, found or "MISSING")
        ok &= bool(found)
    try:
        email = sheets.service_account_email()
        log.info("sheet SA email: %s", email)
        sheets.ensure_tabs()
        log.info("sheet access  : OK (pending rows: %s)", sheets.pending_count())
    except Exception as exc:
        log.error("sheet access  : FAILED - %s", exc)
        ok = False
    try:
        from src.youtube import build_client
        build_client(log)
        log.info("youtube auth  : OK")
    except Exception as exc:
        log.error("youtube auth  : FAILED - %s", exc)
        ok = False
    if not os.environ.get("GCP_SA_JSON") and not Path(cfg.google.service_account_file).exists():
        log.error("service account json: MISSING")
        ok = False
    log.info("RESULT: %s", "all checks passed" if ok else "some checks FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log = setup_logging(verbose=args.verbose)
    cfg = AppConfig.load(args.config)
    log.info("Config loaded (sheet=%s, tz=%s, gap=%s min)",
             cfg.google.sheet_id or "<unset>", cfg.youtube.timezone, cfg.youtube.min_gap_minutes)

    # offline mode: a single URL with --dry-run needs no Google credentials at all
    offline = bool(args.url and args.dry_run)
    if offline:
        log.info("Offline dry-run: Google Sheets is bypassed (nothing will be written).")
        sheets = NullQueue(log)
    else:
        sheets = SheetQueue(cfg.google, log)

    if args.check:
        return health_check(cfg, sheets, log)  # type: ignore[arg-type]

    sheets.ensure_tabs()

    if args.add_url:
        added = sheets.add_url(args.add_url)
        log.info("Queued." if added else "URL already present.")
        return 0

    if args.url:
        pipe = Pipeline(cfg, sheets, log, dry_run=args.dry_run)
        stats = pipe.run_single(args.url)
        log.info("Single-URL result: %s", stats.line())
        return 0 if stats.failed == 0 else 1

    if not sheets.acquire_lock(ttl_minutes=45):
        log.warning("Another run appears to be active - exiting to avoid double uploads.")
        return 0
    try:
        pipe = Pipeline(cfg, sheets, log, dry_run=args.dry_run)
        stats = pipe.run(limit=args.limit)
        return 0 if stats.failed == 0 else 2
    finally:
        sheets.release_lock()


if __name__ == "__main__":
    sys.exit(main())
