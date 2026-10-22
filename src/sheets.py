"""Google Sheets queue backend.

Tabs created automatically:
  Pending   -> the queue you paste URLs into
  Uploaded  -> finished rows are MOVED here (with the YouTube link + publish time)
  Failed    -> rows that exhausted their retries
  Log       -> one line per meaningful event (audit trail)
  State     -> lock + bookkeeping so two runs never overlap
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import GoogleConfig

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS: dict[str, list[str]] = {
    "pending": ["URL", "Status", "Attempts", "Added At (UTC)", "Last Error"],
    "uploaded": [
        "Instagram URL", "YouTube URL", "Video ID", "Title",
        "Publish At (UTC)", "Uploaded At (UTC)", "Duration (s)", "IG Author",
    ],
    "failed": ["Instagram URL", "Error", "Attempts", "Moved At (UTC)"],
    "log": ["Timestamp (UTC)", "Level", "Message"],
    "state": ["Key", "Value"],
}

STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_ERROR = "ERROR"          # transient failure, will retry
STATUS_UPLOADED = "UPLOADED"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> datetime | None:
    """Parse timestamps written by this bot (and tolerate Sheets' own formats)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" UTC", "").replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class PendingItem:
    row: int
    url: str
    status: str
    attempts: int
    error: str


class SheetQueue:
    # ------------------------------------------------------------------ setup
    def __init__(self, cfg: GoogleConfig, logger: logging.Logger | None = None):
        self.cfg = cfg
        self.log_ = logger or logging.getLogger(__name__)
        self._client = None
        self._spreadsheet = None
        self._cache: dict[str, Any] = {}

    # -- credentials ---------------------------------------------------------
    def _credentials(self):
        from google.oauth2.service_account import Credentials as SACredentials

        blob = os.environ.get("GCP_SA_JSON", "").strip()
        if blob:
            info = json.loads(blob)
            return SACredentials.from_service_account_info(info, scopes=SCOPES)
        path = Path(self.cfg.service_account_file or "service_account.json")
        if path.exists():
            return SACredentials.from_service_account_file(str(path), scopes=SCOPES)
        raise RuntimeError(
            "No Google service-account credentials found. Set GCP_SA_JSON or place "
            f"{path} next to main.py (see README step 3)."
        )

    @property
    def client(self):
        if self._client is None:
            import gspread

            self._client = gspread.authorize(self._credentials())
        return self._client

    @property
    def sheet(self):
        if self._spreadsheet is None:
            if not self.cfg.sheet_id:
                raise RuntimeError("google.sheet_id / SHEET_ID is not configured.")
            self._spreadsheet = self.client.open_by_key(self.cfg.sheet_id)
        return self._spreadsheet

    def service_account_email(self) -> str:
        """Handy when you need to know who to share the spreadsheet with."""
        blob = os.environ.get("GCP_SA_JSON", "").strip()
        if blob:
            return json.loads(blob).get("client_email", "?")
        path = Path(self.cfg.service_account_file or "service_account.json")
        if path.exists():
            return json.loads(path.read_text()).get("client_email", "?")
        return "?"

    def ws(self, which: str):
        if which in self._cache:
            return self._cache[which]
        name = {
            "pending": self.cfg.pending_tab,
            "uploaded": self.cfg.uploaded_tab,
            "failed": self.cfg.failed_tab,
            "log": self.cfg.log_tab,
            "state": self.cfg.state_tab,
        }[which]
        ws = self.sheet.worksheet(name)
        self._cache[which] = ws
        return ws

    def ensure_tabs(self) -> None:
        existing = {w.title: w for w in self.sheet.worksheets()}
        mapping = {
            "pending": self.cfg.pending_tab,
            "uploaded": self.cfg.uploaded_tab,
            "failed": self.cfg.failed_tab,
            "log": self.cfg.log_tab,
            "state": self.cfg.state_tab,
        }
        for key, name in mapping.items():
            ws = existing.get(name)
            if ws is None:
                ws = self.sheet.add_worksheet(title=name, rows=1000, cols=max(6, len(HEADERS[key])))
                ws.append_row(HEADERS[key], value_input_option="RAW")
                try:
                    ws.format(f"A1:{chr(64 + len(HEADERS[key]))}1", {"textFormat": {"bold": True}})
                except Exception:  # formatting is cosmetic only
                    pass
                self.log_.info("Created tab %s", name)
            self._cache[key] = ws
        # remove the default empty "Sheet1" if we created everything fresh
        try:
            first = self.sheet.worksheet("Sheet1")
            values = first.get_all_values()
            if len(values) <= 1 and not any(any(c.strip() for c in row) for row in values):
                self.sheet.del_worksheet(first)
        except Exception:
            pass

    # ---------------------------------------------------------------- locking
    def acquire_lock(self, ttl_minutes: int = 30) -> bool:
        """Simple single-flight lock stored in the State tab."""
        ws = self.ws("state")
        rows = {r[0]: (r[1] if len(r) > 1 else "") for r in (ws.get_all_values() or []) if r}
        holder = parse_ts(rows.get("LockTime"))
        if holder and utcnow() - holder < timedelta(minutes=ttl_minutes):
            self.log_.warning("Another run holds the lock until %s (+%s min)", iso(holder), ttl_minutes)
            return False
        self._state_set(ws, "LockTime", iso(utcnow()))
        self._state_set(ws, "LockOwner", os.environ.get("GITHUB_RUN_ID", "local"))
        return True

    def release_lock(self) -> None:
        try:
            ws = self.ws("state")
            self._state_set(ws, "LockTime", "")
            self._state_set(ws, "LockOwner", "")
        except Exception as exc:  # pragma: no cover
            self.log_.warning("Could not release lock: %s", exc)

    @staticmethod
    def _state_set(ws, key: str, value: str) -> None:
        values = ws.get_all_values() or []
        for idx, row in enumerate(values, start=1):
            if row and row[0] == key:
                ws.update(f"B{idx}", [[value]], value_input_option="RAW")
                return
        ws.append_row([key, value], value_input_option="RAW")

    # ------------------------------------------------------------- queue read
    def list_pending(self) -> list[PendingItem]:
        ws = self.ws("pending")
        values = ws.get_all_values() or []
        items: list[PendingItem] = []
        for idx, row in enumerate(values[1:], start=2):
            row = row + [""] * (5 - len(row))
            url = (row[0] or "").strip()
            status = (row[1] or "").strip().upper()
            if not url or status in {STATUS_UPLOADED, "DONE", "SKIP", "SKIPPED"}:
                continue
            if url.startswith("#"):  # commented-out row
                continue
            try:
                attempts = int(float(row[2])) if (row[2] or "").strip() else 0
            except ValueError:
                attempts = 0
            items.append(PendingItem(row=idx, url=url, status=status, attempts=attempts, error=row[4] or ""))
        return items

    def set_status(self, row: int, status: str, error: str = "") -> None:
        """Update Status + Last Error only (never touches Attempts / Added At)."""
        ws = self.ws("pending")
        ws.update(f"B{row}", [[status]], value_input_option="RAW")
        ws.update(f"E{row}", [[error[:400]]], value_input_option="RAW")

    def set_status_with_attempts(self, row: int, status: str, attempts: int, error: str = "") -> None:
        ws = self.ws("pending")
        ws.update(f"B{row}:C{row}", [[status, attempts]], value_input_option="RAW")
        ws.update(f"E{row}", [[error[:400]]], value_input_option="RAW")

    def reset_stale_processing(self) -> int:
        """A crashed run can leave rows stuck on PROCESSING - put them back."""
        ws = self.ws("pending")
        values = ws.get_all_values() or []
        fixed = 0
        for idx, row in enumerate(values[1:], start=2):
            if row and len(row) > 1 and (row[1] or "").strip().upper() == STATUS_PROCESSING:
                ws.update(f"B{idx}", [[STATUS_PENDING]], value_input_option="RAW")
                fixed += 1
        if fixed:
            self.log_.info("Reset %s stale PROCESSING row(s)", fixed)
        return fixed

    def add_url(self, url: str) -> bool:
        url = url.strip()
        if not url:
            return False
        ws = self.ws("pending")
        existing = {(r[0] or "").strip().rstrip("/") for r in (ws.get_all_values() or [])[1:] if r}
        if url.rstrip("/") in existing:
            self.log_.info("URL already queued: %s", url)
            return False
        ws.append_row(
            [url, STATUS_PENDING, 0, iso(utcnow()), ""],
            value_input_option="RAW",
        )
        return True

    def _pending_row_of(self, url: str) -> int | None:
        ws = self.ws("pending")
        for idx, row in enumerate(ws.get_all_values() or [], start=1):
            if row and (row[0] or "").strip() == url.strip():
                return idx
        return None

    def move_to_uploaded(self, url: str, record: Iterable[Any]) -> None:
        self.ws("uploaded").append_row(list(record), value_input_option="RAW")
        row = self._pending_row_of(url)
        if row:
            self.ws("pending").delete_rows(row)

    def move_to_failed(self, url: str, error: str, attempts: int) -> None:
        self.ws("failed").append_row(
            [url, (error or "")[:400], attempts, iso(utcnow())], value_input_option="RAW"
        )
        row = self._pending_row_of(url)
        if row:
            self.ws("pending").delete_rows(row)

    # ------------------------------------------------------------ statistics
    def uploads_on(self, local_date, tz) -> int:
        """How many videos were uploaded on a given local date (quota guard)."""
        count = 0
        for row in (self.ws("uploaded").get_all_values() or [])[1:]:
            if not row:
                continue
            ts = parse_ts(row[5] if len(row) > 5 else "")
            if ts and ts.astimezone(tz).date() == local_date:
                count += 1
        return count

    def last_publish_at(self) -> datetime | None:
        """Latest scheduled publish time across everything already uploaded."""
        latest: datetime | None = None
        for row in (self.ws("uploaded").get_all_values() or [])[1:]:
            if not row:
                continue
            ts = parse_ts(row[4] if len(row) > 4 else "")
            if ts and (latest is None or ts > latest):
                latest = ts
        return latest

    def pending_count(self) -> int:
        return len(self.list_pending())

    def append_log(self, level: str, message: str) -> None:
        try:
            ws = self.ws("log")
            ws.append_row([iso(utcnow()), level, message[:900]], value_input_option="RAW")
            if ws.row_count and len(ws.get_all_values()) > 4000:  # keep the sheet tidy
                ws.delete_rows(2, 1000)
        except Exception as exc:  # never let logging break a run
            self.log_.debug("Sheet log failed: %s", exc)


class NullQueue:
    """In-memory stand-in for SheetQueue, so the pipeline can be tested offline.

    Used by `main.py --url ... --dry-run` before Google credentials exist.
    """

    def __init__(self, logger: logging.Logger | None = None):
        self.log_ = logger or logging.getLogger(__name__)
        self.rows: list = []
        self.uploaded: list[list] = []

    def ensure_tabs(self) -> None:
        pass

    def acquire_lock(self, ttl_minutes: int = 45) -> bool:
        return True

    def release_lock(self) -> None:
        pass

    def pending_count(self) -> int:
        return len(self.rows)

    def reset_stale_processing(self) -> int:
        return 0

    def list_pending(self) -> list:
        return list(self.rows)

    def uploads_on(self, local_date, tz) -> int:
        return 0

    def last_publish_at(self):
        return None

    def set_status(self, *a, **k) -> None:
        pass

    def set_status_with_attempts(self, *a, **k) -> None:
        pass

    def move_to_uploaded(self, url, record) -> None:
        self.uploaded.append(list(record))
        self.rows = [r for r in self.rows if r.url != url]

    def move_to_failed(self, url, error, attempts) -> None:
        self.log_.error("FAILED %s: %s", url, error)

    def append_log(self, level: str, message: str) -> None:
        self.log_.debug("LOG %s: %s", level, message)

    def add_url(self, url: str) -> bool:
        self.log_.info("[offline] would queue %s", url)
        return True

    def ws(self, which: str):
        queue = self

        class _WS:
            def append_row(self, row, **_k):
                queue.uploaded.append(list(row))

            def get_all_values(self):
                return []

        return _WS()
