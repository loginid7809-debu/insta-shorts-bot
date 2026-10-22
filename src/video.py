"""Turn any downloaded video into a strict YouTube Shorts file.

YouTube Shorts requirements enforced here:
  * vertical 9:16 (1080x1920 by default)
  * H.264 video + AAC audio in an MP4 container (yuv420p, faststart)
  * 3 minutes (180 s) or shorter
  * constant frame rate, square pixels (setsar=1)
Non-vertical sources are padded with a blurred copy of themselves (default),
padded with black, or centre-cropped -- configurable via video.pad_mode.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import VideoConfig


class VideoError(RuntimeError):
    """The file could not be produced/parsed."""


class ShortsComplianceError(VideoError):
    """The output exists but would NOT be treated as a Short by YouTube.

    Permanent error: retrying the same source will produce the same result, so the
    pipeline counts attempts and eventually moves the row to the Failed tab.
    """


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool
    vcodec: str = ""
    acodec: str = ""
    rotation: int = 0

    @property
    def ratio(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_vertical_9_16(self) -> bool:
        return abs(self.ratio - 9 / 16) < 0.02

    def summary(self) -> str:
        return (
            f"{self.width}x{self.height}, {self.duration:.1f}s, {self.fps:.0f}fps, "
            f"{self.vcodec or '?'}+{self.acodec or 'no audio'}"
        )


def ffmpeg_binary() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise VideoError("ffmpeg not found on PATH (install it: apt-get install ffmpeg / brew install ffmpeg)")
    return exe


def ffprobe_binary() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise VideoError("ffprobe not found on PATH (it ships with ffmpeg)")
    return exe


def _parse_fps(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/")
            return float(num) / float(den) if float(den) else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> VideoInfo:
    cmd = [
        ffprobe_binary(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VideoError(f"ffprobe failed: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise VideoError("File contains no video stream.")

    rotation = 0
    rotate_tag = (video.get("tags") or {}).get("rotate")
    if rotate_tag:
        try:
            rotation = int(float(rotate_tag))
        except ValueError:
            rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if abs(rotation) % 180 == 90:
        width, height = height, width

    duration = 0.0
    for source in (video.get("duration"), data.get("format", {}).get("duration")):
        try:
            if source:
                duration = float(source)
                break
        except (TypeError, ValueError):
            continue

    return VideoInfo(
        width=width,
        height=height,
        duration=duration,
        fps=_parse_fps(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0")),
        has_audio=audio is not None,
        vcodec=str(video.get("codec_name") or ""),
        acodec=str((audio or {}).get("codec_name") or ""),
        rotation=rotation,
    )


def _video_filter(info: VideoInfo, cfg: VideoConfig) -> tuple[list[str], str | None]:
    """Return (args, filter_complex_or_None)."""
    w, h, fps = cfg.width, cfg.height, cfg.fps

    # Already vertical 9:16 -> plain scale (crop only removes sub-pixel rounding).
    if info.is_vertical_9_16 or cfg.pad_mode == "crop":
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h},fps={fps},setsar=1,format=yuv420p"
        )
        return ["-vf", vf], None

    if cfg.pad_mode == "black":
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},setsar=1,format=yuv420p"
        )
        return ["-vf", vf], None

    # default: blurred background pillarbox
    graph = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h},gblur=sigma=28,eq=brightness=-0.08[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2:shortest=1,fps={fps},setsar=1,format=yuv420p[v]"
    )
    return [], graph


def assert_shorts_compliant(info: VideoInfo, cfg: VideoConfig) -> None:
    """Hard gate: refuse to upload anything YouTube would not accept as a Short.

    Shorts eligibility (2026): vertical or square aspect ratio and duration
    between 1 second and 3 minutes. We additionally pin 1080x1920 H.264/AAC so
    every upload is byte-for-byte in the expected format.
    """
    problems: list[str] = []
    if (info.width, info.height) != (cfg.width, cfg.height):
        problems.append(f"resolution is {info.width}x{info.height}, expected {cfg.width}x{cfg.height}")
    if info.height and info.width / info.height > 1.0 + 1e-6:
        problems.append(f"aspect ratio {info.width}x{info.height} is horizontal (Shorts must be vertical or square)")
    if info.duration < cfg.min_duration_sec:
        problems.append(f"duration {info.duration:.2f}s is below the {cfg.min_duration_sec}s minimum")
    if info.duration > cfg.max_duration_sec + 1.5:
        problems.append(f"duration {info.duration:.1f}s exceeds the {cfg.max_duration_sec}s Shorts limit")
    if not info.has_audio:
        problems.append("no audio stream (YouTube requires one)")
    if info.vcodec != "h264":
        problems.append(f"video codec is {info.vcodec}, expected h264")
    if info.acodec and info.acodec != "aac":
        problems.append(f"audio codec is {info.acodec}, expected aac")
    if problems:
        raise ShortsComplianceError("Not Shorts-compliant: " + "; ".join(problems))


def to_shorts(src: Path, dst: Path, cfg: VideoConfig, logger: logging.Logger | None = None) -> VideoInfo:
    """Re-encode *src* into a Shorts-compliant MP4 at *dst*; return output info."""
    logger = logger or logging.getLogger(__name__)
    info = probe(src)
    logger.info("Source: %s", info.summary())

    args, graph = _video_filter(info, cfg)
    cmd = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]

    add_silence = cfg.ensure_audio and not info.has_audio
    if add_silence:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000"]

    if graph:
        cmd += ["-filter_complex", graph, "-map", "[v]"]
    else:
        cmd += args + ["-map", "0:v:0"]

    if add_silence:
        cmd += ["-map", "1:a:0", "-shortest"]
    elif info.has_audio:
        cmd += ["-map", "0:a:0"]

    cmd += [
        "-c:v", "libx264",
        "-preset", cfg.preset,
        "-crf", str(cfg.crf),
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
    ]
    if info.duration and (info.duration > cfg.max_duration_sec or cfg.max_duration_sec > 0):
        cmd += ["-t", str(cfg.max_duration_sec)]
    cmd += [str(dst)]

    logger.info("Encoding to %sx%s Shorts format...", cfg.width, cfg.height)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise VideoError(f"ffmpeg failed: {(proc.stderr or '').strip()[-700:]}")

    out = probe(dst)
    assert_shorts_compliant(out, cfg)
    logger.info("Encoded + verified: %s (%.1f MB)", out.summary(), dst.stat().st_size / 1_048_576)
    return out
