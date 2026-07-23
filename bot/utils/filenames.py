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


_CODEC_LABEL = {
    "libx264": "H264", "h264": "H264",
    "libx265": "HEVC", "hevc": "HEVC",
    "libsvtav1": "AV1", "libaom-av1": "AV1", "av1": "AV1",
    "copy": "COPY",
}


def build_output_name(source_name: str, *, height: int | None, codec: str,
                      container: str, hardsub: bool = False) -> str:
    title = clean_title(source_name).replace(" ", "_") or "video"
    parts = [title]
    if height:
        parts.append(f"{height}p")
    label = _CODEC_LABEL.get(codec.lower(), codec.upper() if codec else "")
    if label and label != "COPY":
        parts.append(label)
    if hardsub:
        parts.append("HARDSUB")
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
