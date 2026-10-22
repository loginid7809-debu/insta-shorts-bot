"""End-to-end ffmpeg tests: any input shape must become a valid Short."""
from __future__ import annotations

import shutil
import subprocess
import sys
import logging
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import VideoConfig
from src.video import probe, to_shorts

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)

LOG = logging.getLogger("test")


def make_source(path: Path, w: int, h: int, seconds: int = 2, audio: bool = True) -> Path:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=25:duration={seconds}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1080), (720, 1280)])
def test_any_aspect_becomes_1080x1920(tmp_path: Path, size):
    src = make_source(tmp_path / "in.mp4", *size)
    dst = tmp_path / "out.mp4"
    cfg = VideoConfig(crf=30, preset="ultrafast")
    info = to_shorts(src, dst, cfg, LOG)

    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
    assert info.vcodec == "h264"
    assert info.acodec == "aac"
    assert dst.stat().st_size > 1000
    again = probe(dst)
    assert abs(again.ratio - 9 / 16) < 0.01


def test_silent_source_gets_silent_audio_track(tmp_path: Path):
    src = make_source(tmp_path / "silent.mp4", 1080, 1920, audio=False)
    dst = tmp_path / "out.mp4"
    info = to_shorts(src, dst, VideoConfig(crf=30, preset="ultrafast"), LOG)
    assert info.has_audio


def test_long_video_is_trimmed(tmp_path: Path):
    src = make_source(tmp_path / "long.mp4", 720, 1280, seconds=8)
    dst = tmp_path / "out.mp4"
    cfg = VideoConfig(crf=30, preset="ultrafast", max_duration_sec=3)
    info = to_shorts(src, dst, cfg, LOG)
    assert info.duration <= 4.5


@pytest.mark.parametrize("mode", ["black", "crop", "blur"])
def test_pad_modes_produce_valid_output(tmp_path: Path, mode):
    src = make_source(tmp_path / "in.mp4", 1920, 1080)
    dst = tmp_path / "out.mp4"
    cfg = VideoConfig(crf=30, preset="ultrafast", pad_mode=mode)
    info = to_shorts(src, dst, cfg, LOG)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
