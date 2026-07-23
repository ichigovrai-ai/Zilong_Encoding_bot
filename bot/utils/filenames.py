"""Safe output filenames.

Sanitises user/torrent-supplied names for the local filesystem, strips release
clutter for display, and builds predictable output names like
`Episode_01_720p_HEVC.mkv` without ever allowing path traversal or collisions.
"""
from __future__ import annotations

import re
from pathlib import Path

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}
_MAX_STEM = 120

_CLUTTER = re.compile(
    r"""
    \[[^\]]*\] | \{[^}]*\} | \([^)]*\)          # bracketed release tags
    | \b(?:480|540|576|720|1080|1440|2160)p\b   # resolution labels
    | \b4k\b | \buhd\b | \bhdr10?\b
    | \b(?:x|h)\.?26[456]\b | \bhevc\b | \bavc\b | \bav1\b
    | \b(?:web-?dl|webrip|bluray|bdrip|brrip|hdtv|dvdrip|remux)\b
    | \b(?:aac|opus|flac|ac3|eac3|dts|ddp?[257]\.?[01]?)\b
    | \b(?:10|8)-?bit\b
    | \b(?:multi|dual|vostfr|vosta|subbed|dubbed)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def sanitize_filename(name: str, default: str = "file") -> str:
    """Make a name safe as a single path component. Preserves Unicode."""
    name = (name or "").strip().replace("‮", "")  # RTL-override spoofing
    name = name.replace("..", ".")
    name = _UNSAFE.sub("_", name)
    name = name.strip(". ")
    if not name:
        return default
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.lower() in _RESERVED:
        stem = f"_{stem}"
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM].rstrip("._ ")
    return f"{stem}.{ext}" if ext else stem


def clean_title(name: str) -> str:
    """Strip release-group clutter for captions/output names, keep the title."""
    stem = Path(name).stem
    cleaned = _CLUTTER.sub(" ", stem)
    cleaned = re.sub(r"[._]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")
    return cleaned or Path(sanitize_filename(name)).stem


_LANG_TAGS = {
    "fr": "VOSTFR", "fra": "VOSTFR", "fre": "VOSTFR",
    "en": "VOSTEN", "eng": "VOSTEN",
    "es": "VOSTES", "spa": "VOSTES",
    "pt": "VOSTPT", "por": "VOSTPT",
    "ar": "VOSTAR", "ara": "VOSTAR",
    "de": "VOSTDE", "ger": "VOSTDE", "deu": "VOSTDE",
    "it": "VOSTIT", "ita": "VOSTIT",
}


def lang_tag(lang: str) -> str:
    """Fansub-style language tag for a burned subtitle, e.g. fr → VOSTFR."""
    lang = (lang or "").strip().lower()
    if not lang or lang == "und":
        return "VOSTFR"          # sensible default for this bot's audience
    return _LANG_TAGS.get(lang, "VOST" + lang[:2].upper())


def _title_part(raw_title: str) -> str:
    """Tidy a title prefix into Title_Case_With_Underscores."""
    s = _CLUTTER.sub(" ", raw_title)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"[^\w\s-]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s{2,}", " ", s).strip(" -_")
    return s.replace(" ", "_")


def build_output_name(source_name: str, *, container: str = "mp4",
                      hardsub: bool = False, sub_lang: str = "",
                      height: int | None = None) -> str:
    """Clean output name: ``Title_SxxExx_VOSTFR.mp4``.

    Drops release-group junk, codec labels and source tags. Season/episode
    become ``SxxExx`` (season assumed 01 when the release omits it, as anime
    seasonal releases do). A hardsub adds the subtitle-language tag. Files with
    no detectable episode fall back to ``Title_<height>p`` so they stay
    distinct.
    """
    from bot.utils.matching import split_title_episode
    raw_title, ep = split_title_episode(source_name)
    title = _title_part(raw_title) or "video"
    parts = [title]
    if ep:
        season = ep.season if ep.season is not None else 1
        parts.append(f"S{season:02d}E{ep.episode:02d}")
    elif height:
        parts.append(f"{height}p")
    if hardsub:
        parts.append(lang_tag(sub_lang))
    name = sanitize_filename("_".join(p for p in parts if p))
    return f"{name}.{container.lstrip('.')}"


def unique_path(path: Path) -> Path:
    """Return `path`, or `stem_2.ext` … if it already exists."""
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Cannot find a free name for {path}")
