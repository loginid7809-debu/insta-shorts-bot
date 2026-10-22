"""Classification of download failures: transient vs permanent."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.downloader import classify_error
from src.errors import TransientError


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [Instagram] Cxxx: Instagram sent an empty media response. See https://github.com/yt-dlp",
        "ERROR: [Instagram] Cxxx: Requested content is not available, rate-limit hit",
        "ERROR: [Instagram] Cxxx: login required",
        "ERROR: Unable to download webpage: <urlopen error timed out>",
        "ERROR: HTTP Error 503: Service Unavailable",
        "ERROR: Connection reset by peer",
    ],
)
def test_blocked_or_network_errors_are_transient(message):
    _, transient = classify_error(RuntimeError(message))
    assert transient is True


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [Instagram] Cxxx: This content is from a private account",
        "ERROR: Unsupported URL: https://www.instagram.com/username/",
        "ERROR: [Instagram] Cxxx: Video is not available",
        "ERROR: [Instagram] Cxxx: Post not found",
    ],
)
def test_rights_and_url_problems_are_permanent(message):
    _, transient = classify_error(RuntimeError(message))
    assert transient is False


def test_unknown_errors_are_optimistic():
    """Better to retry an unknown failure than to blacklist a good video."""
    _, transient = classify_error(RuntimeError("ERROR: something nobody has seen before"))
    assert transient is True


def test_messages_have_no_help_url_noise():
    message, _ = classify_error(
        RuntimeError("ERROR: [Instagram] Cxxx: Instagram sent an empty media response. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ for details")
    )
    assert "https://github.com" not in message
    assert "Instagram" in message


def test_transient_error_type_flag():
    assert TransientError.transient is True
