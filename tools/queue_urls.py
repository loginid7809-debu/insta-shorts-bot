"""Bulk-add Instagram URLs to the Pending tab.

    python tools/queue_urls.py links.txt          # one URL per line
    cat links.txt | python tools/queue_urls.py    # from stdin

Blank lines and lines starting with # are ignored. Duplicates are skipped.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import AppConfig              # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402
from src.sheets import SheetQueue            # noqa: E402


def read_urls(arg: str | None) -> list[str]:
    if arg and arg != "-":
        text = Path(arg).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Queue Instagram URLs into the sheet.")
    ap.add_argument("file", nargs="?", default="-", help="text file with one URL per line (default: stdin)")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    log = setup_logging()
    cfg = AppConfig.load(args.config)
    queue = SheetQueue(cfg.google, log)
    queue.ensure_tabs()

    urls = read_urls(args.file)
    added = sum(1 for url in urls if queue.add_url(url))
    log.info("Queued %s new URL(s) out of %s provided. Pending now: %s",
             added, len(urls), queue.pending_count())
    return 0


if __name__ == "__main__":
    sys.exit(main())
