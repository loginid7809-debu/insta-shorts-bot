"""Pretty, timestamped logging that also works well inside GitHub Actions."""
from __future__ import annotations

import logging
import sys


class GitHubFormatter(logging.Formatter):
    """Adds ::error:: / ::warning:: workflow commands so failures are highlighted."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if record.levelno >= logging.ERROR:
            return f"::error::{text}"
        if record.levelno >= logging.WARNING:
            return f"::warning::{text}"
        return text


def setup_logging(verbose: bool = False, log_file: str = "run.log") -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s | %(levelname)-7s | %(message)s"
    root = logging.getLogger()
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(GitHubFormatter(fmt, "%H:%M:%S"))
    root.addHandler(console)

    try:  # keep a copy on disk so GitHub Actions can upload it as an artifact
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(file_handler)
    except OSError:
        pass

    root.setLevel(level)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger("shorts")
