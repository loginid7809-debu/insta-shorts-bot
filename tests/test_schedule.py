"""Tests for the hourly publish ladder + timestamp parsing."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import YouTubeConfig
from src.sheets import parse_ts
from src.youtube import format_publish_at, next_publish_slot

IST = timezone(timedelta(hours=5, minutes=30))


def dt(y, m, d, hh, mm=0, tz=timezone.utc):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def test_first_slot_uses_buffer_only():
    cfg = YouTubeConfig(publish_buffer_minutes=15, min_gap_minutes=60)
    now = dt(2026, 10, 22, 4, 0)
    slot = next_publish_slot(now, None, cfg)
    assert slot == dt(2026, 10, 22, 4, 15)


def test_second_slot_is_one_hour_after_previous_even_if_upload_is_late():
    cfg = YouTubeConfig(publish_buffer_minutes=15, min_gap_minutes=60)
    previous = dt(2026, 10, 22, 4, 15)
    now = dt(2026, 10, 22, 4, 20)          # uploaded only 5 minutes later
    slot = next_publish_slot(now, previous, cfg)
    assert slot == dt(2026, 10, 22, 5, 15)  # 60 min after previous, not 4:35


def test_ladder_walks_hourly_for_ten_videos():
    cfg = YouTubeConfig(publish_buffer_minutes=15, min_gap_minutes=60)
    now = dt(2026, 10, 22, 19, 0)          # 00:30 IST
    last = None
    slots = []
    for _ in range(10):
        last = next_publish_slot(now, last, cfg)
        slots.append(last)
        now = last - timedelta(minutes=59)  # next upload happens right before its slot
    gaps = {(b - a) for a, b in zip(slots, slots[1:])}
    assert gaps == {timedelta(hours=1)}
    assert slots[0].astimezone(IST).hour == 0
    assert slots[1].astimezone(IST).hour == 1


def test_no_slot_in_the_past():
    cfg = YouTubeConfig(publish_buffer_minutes=15, min_gap_minutes=60)
    now = dt(2026, 10, 22, 12, 0)
    slot = next_publish_slot(now, dt(2026, 10, 20, 1, 0), cfg)
    assert slot >= now


def test_publish_at_format_matches_youtube():
    value = format_publish_at(dt(2026, 10, 22, 4, 15))
    assert value == "2026-10-22T04:15:00.000Z"


def test_parse_ts_accepts_bot_and_sheet_formats():
    assert parse_ts("2026-10-22T04:15:00Z") == dt(2026, 10, 22, 4, 15)
    assert parse_ts("2026-10-22 04:15:00") == dt(2026, 10, 22, 4, 15)
    assert parse_ts("") is None
    assert parse_ts(None) is None
