"""Unit tests for caption -> YouTube metadata conversion."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import MetadataConfig
from src.metadata import build_metadata, clean_caption, extract_hashtags


CAPTION = """This dog has better dance moves than me 😂🔥

Follow for more!
link in bio
#dance #dog #funny
https://instagram.com/something
"""


def test_hashtags_extracted_and_deduped():
    tags = extract_hashtags("#Dance #dog #dance #funny")
    assert tags == ["dance", "dog", "funny"]


def test_clean_caption_removes_urls_hashtags_and_cta():
    cleaned = clean_caption(CAPTION)
    assert "instagram.com" not in cleaned
    assert "#dance" not in cleaned
    assert "link in bio" not in cleaned.lower()
    assert "better dance moves" in cleaned


def test_title_gets_shorts_suffix_and_length_limit():
    meta = build_metadata(CAPTION, MetadataConfig())
    assert meta.title.endswith("#shorts")
    assert len(meta.title) <= 100


def test_long_caption_is_truncated_cleanly():
    long_text = "word " * 200
    meta = build_metadata(long_text, MetadataConfig())
    assert len(meta.title) <= 100
    assert meta.title.endswith("#shorts")
    assert "word word" in meta.title


def test_description_contains_caption_credit_and_hashtag_block():
    meta = build_metadata(CAPTION, MetadataConfig(), uploader="some.creator")
    assert "#shorts" in meta.description.lower()
    assert "Credit: @some.creator" in meta.description
    assert "#dance" in meta.description
    assert meta.description.count("#shorts") >= 1


def test_tags_respect_500_char_budget():
    cfg = MetadataConfig(default_hashtags=[f"tag{i}" for i in range(200)])
    meta = build_metadata(CAPTION, cfg)
    assert sum(len(t) + 1 for t in meta.tags) <= 500
    assert all(len(t) <= 100 for t in meta.tags)


def test_empty_caption_uses_fallback_title():
    meta = build_metadata("", MetadataConfig(fallback_title="Fallback here"))
    assert meta.title == "Fallback here #shorts"
    assert "#shorts" in meta.description


def test_no_double_shorts_suffix():
    meta = build_metadata("My clip #shorts", MetadataConfig())
    assert meta.title.lower().count("#shorts") == 1


def test_instagram_specific_chars_are_safe_for_tags():
    meta = build_metadata("Fun <script> & co", MetadataConfig())
    assert all("<" not in t and "&" not in t for t in meta.tags)


def test_description_never_exceeds_youtube_limit():
    cfg = MetadataConfig(max_description_len=300)
    meta = build_metadata("x" * 2000, cfg)
    assert len(meta.description) <= 300
