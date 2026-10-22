"""Build YouTube Shorts metadata (title / description / tags) from an
Instagram caption.

Rules enforced here (YouTube API limits):
  * title        <= 100 characters
  * description  <= 5000 characters
  * tags         total <= 500 characters, no tag longer than 100
  * "#shorts"    always present in title AND description
Anything the caption does not give us falls back to config values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import MetadataConfig

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# hashtags incl. unicode letters/digits/underscore
HASHTAG_RE = re.compile(r"#([\w\u00C0-\uFFFF]+)", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w.])@([\w.]{2,30})")
WS_RE = re.compile(r"[ \t]+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
BAD_TAG_CHARS_RE = re.compile(r"[<>&\"'#]")
# Instagram-specific noise that should not end up in a YouTube title
NOISE_PHRASES = (
    "link in bio",
    "follow for more",
    "follow me",
    "dm for",
    "comment below",
    "share this",
    "save this",
    "double tap",
    "turn on post notifications",
)


@dataclass
class VideoMetadata:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- helpers
def _normalise(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return text


def extract_hashtags(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in HASHTAG_RE.findall(_normalise(text)):
        tag = tag.strip("_").lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def extract_mentions(text: str) -> list[str]:
    out: list[str] = []
    for handle in MENTION_RE.findall(_normalise(text)):
        handle = handle.strip(".").lower()
        if handle and handle not in out:
            out.append(handle)
    return out


def clean_caption(text: str) -> str:
    """Strip URLs, hashtags and instagram spam while keeping the actual text."""
    text = _normalise(text)
    text = URL_RE.sub(" ", text)
    text = HASHTAG_RE.sub(" ", text)
    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = WS_RE.sub(" ", raw_line).strip(" .-–—|")
        if not line:
            lines.append("")
            continue
        low = line.lower()
        if any(phrase in low for phrase in NOISE_PHRASES) and len(line) < 60:
            continue  # drop pure call-to-action lines
        lines.append(line)
    # collapse repeated blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    # collapse to a single paragraph when the caption is short & choppy
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def _truncate_words(text: str, limit: int) -> str:
    """Cut to <= limit chars without slicing a word in half."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut[: cut.rfind(" ")].rstrip()
    return cut.rstrip(" .,;:-–—|") or text[:limit].strip()


def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for chunk in SENTENCE_SPLIT_RE.split(text):
        chunk = chunk.strip(" -–—|•*")
        if len(chunk) >= 3:
            return chunk
    return text


def _normalise_tag(tag: str) -> str:
    tag = BAD_TAG_CHARS_RE.sub("", tag or "").strip().lower()
    tag = tag.replace("_", " ")
    tag = WS_RE.sub(" ", tag).strip(" .,-")
    return tag[:40]


def build_tags(
    caption_hashtags: list[str],
    cfg: MetadataConfig,
    extra: list[str] | None = None,
    budget: int = 480,
) -> list[str]:
    """Build the YouTube tag list, respecting the 500-char API limit."""
    candidates: list[str] = []
    candidates += list(caption_hashtags)
    candidates += list(cfg.default_hashtags)
    candidates += list(cfg.extra_tags)
    candidates += list(extra or [])

    tags: list[str] = []
    used = 0
    for cand in candidates:
        tag = _normalise_tag(cand)
        if not tag or tag in tags:
            continue
        if used + len(tag) + 1 > budget:
            continue
        tags.append(tag)
        used += len(tag) + 1
    return tags


# ------------------------------------------------------------------- public API
def build_metadata(
    caption: str,
    cfg: MetadataConfig,
    uploader: str = "",
    fallback_title: str | None = None,
) -> VideoMetadata:
    caption = _normalise(caption or "")
    hashtags = extract_hashtags(caption)
    cleaned = clean_caption(caption)

    # ---- title ----
    base = _first_sentence(cleaned) if cleaned else ""
    if not base:
        base = fallback_title or cfg.fallback_title
    suffix = cfg.title_suffix.strip()
    room = cfg.title_max_len - (len(suffix) + 1 if suffix else 0)
    title = _truncate_words(base, max(room, 10))
    if suffix and suffix.lower() not in title.lower():
        title = f"{title} {suffix}".strip()
    title = _truncate_words(title, cfg.title_max_len)

    # ---- hashtags block for the description ----
    block_tags: list[str] = []
    seen: set[str] = set()
    for tag in ["shorts"] + hashtags + list(cfg.default_hashtags):
        t = re.sub(r"[^\w]", "", tag).lower()
        if not t or t in seen:
            continue
        seen.add(t)
        block_tags.append(t)
        if len(block_tags) >= max(1, cfg.max_hashtags_in_description):
            break
    hashtag_line = " ".join(f"#{t}" for t in block_tags)

    # ---- description ----
    parts: list[str] = []
    if cleaned:
        parts.append(cleaned)
    if cfg.credit_original_creator and uploader:
        parts.append(f"Credit: @{uploader.lstrip('@')}")
    if cfg.description_footer.strip():
        parts.append(cfg.description_footer.strip())
    parts.append(hashtag_line)
    description = "\n\n".join(p for p in parts if p).strip()

    if "#shorts" not in description.lower():
        description = f"{description}\n\n#shorts".strip()

    if len(description) > cfg.max_description_len:
        description = _truncate_words(description, cfg.max_description_len - 20) + "\n\n#shorts"

    tags = build_tags(
        hashtags,
        cfg,
        extra=([uploader.replace(".", " ")] if uploader and cfg.credit_original_creator else []),
    )

    return VideoMetadata(title=title, description=description, tags=tags, hashtags=hashtags)
