"""Create/verify the Google Sheet tabs for the queue.

    python tools/setup_sheet.py --sheet-id <SHEET_ID>

The spreadsheet must already be shared with the service-account email
(Editor). Find that email with:

    python tools/setup_sheet.py --show-email
   (or: jq -r .client_email service_account.json)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import AppConfig          # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402
from src.sheets import SheetQueue        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare the queue spreadsheet.")
    ap.add_argument("--sheet-id", default=None, help="spreadsheet id or full URL")
    ap.add_argument("--show-email", action="store_true", help="print the service-account email")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    log = setup_logging()
    cfg = AppConfig.load(args.config)
    if args.sheet_id:
        sid = args.sheet_id.strip()
        if "/" in sid:                      # accept a full Sheets URL
            sid = sid.split("/d/")[1].split("/")[0]
        cfg.google.sheet_id = sid

    queue = SheetQueue(cfg.google, log)

    if args.show_email:
        print(queue.service_account_email())
        return 0

    if not cfg.google.sheet_id:
        log.error("No sheet id. Pass --sheet-id or set SHEET_ID.")
        return 1

    try:
        queue.ensure_tabs()
        log.info("Tabs ready in spreadsheet %s", cfg.google.sheet_id)
        log.info("Pending rows: %s | Uploaded: %s | Failed: %s",
                 queue.pending_count(),
                 max(len(queue.ws("uploaded").get_all_values()) - 1, 0),
                 max(len(queue.ws("failed").get_all_values()) - 1, 0))
    except Exception as exc:
        log.error("Could not open the spreadsheet: %s", exc)
        log.error("Did you share it with %s (Editor)?", queue.service_account_email())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
