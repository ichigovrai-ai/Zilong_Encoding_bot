"""Extracting embedded streams: subtitles and audio.

Bitmap subtitles (PGS/VobSub) cannot be converted to text without OCR, which
is not implemented — they are reported as unsupported for hardsub-by-text and
can only be extracted in their native form inside an .mks/.sup container.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot.services.ffprobe import MediaInfo, SubtitleStream
from bot.utils.subprocesses import CancelToken, run_process, tail_text

log = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    pass


async def extract_text_subtitle(
    input_path: str, stream: SubtitleStream, out_dir: str | Path, *,
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
    timeout: float = 300,
) -> str:
    """Extract an embedded TEXT subtitle stream to .ass (preserves styling for
    ASS sources; SRT/VTT convert losslessly enough for burn-in)."""
    if stream.is_bitmap:
        raise ExtractionError(
            "This is a bitmap subtitle (PGS/VobSub). It cannot be hardsubbed "
            "as text — OCR is not supported."
        )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # ASS sources stay ASS; everything else becomes SRT to avoid surprising
    # style synthesis.
    ext = "ass" if stream.codec in ("ass", "ssa") else "srt"
    out = out_dir / f"embedded_{stream.type_index}.{ext}"
    args = [ffmpeg, "-hide_banner", "-y"]
    if "://" in input_path:
        # Remote input (e.g. a signed Seedr URL): CDNs often reject ffmpeg's
        # default user agent. Pass a longer `timeout` for big remote files.
        args += ["-user_agent", "Mozilla/5.0"]
    args += ["-i", input_path,
             "-map", f"0:s:{stream.type_index}", "-c:s", ext, str(out)]
    code, _, err = await run_process(args, timeout=timeout, cancel_token=cancel_token)
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        raise ExtractionError(
            f"Could not extract subtitle stream: {tail_text(err, 300)}"
        )
    return str(out)


async def extract_audio(
    input_path: str, audio_type_index: int, out_dir: str | Path, *,
    codec: str = "copy", container: str = "mka",
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
) -> str:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"audio_{audio_type_index}.{container}"
    args = [ffmpeg, "-hide_banner", "-y", "-i", input_path,
            "-map", f"0:a:{audio_type_index}", "-c:a", codec, "-vn", str(out)]
    code, _, err = await run_process(args, timeout=1800, cancel_token=cancel_token)
    if code != 0 or not out.exists():
        raise ExtractionError(f"Audio extraction failed: {tail_text(err, 300)}")
    return str(out)


async def convert_subtitle_format(
    raw_path: str, target: str, out_dir: str | Path, *,
    output_stem: str = "subtitle", fps: float = 23.976,
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
) -> str:
    """Convert any supported subtitle file to srt/ass/vtt.

    The input goes through the normal preparation pipeline first (encoding
    detection, content-based format sniffing, MicroDVD/LRC conversion,
    validation) so a mislabeled .txt converts just as well as a real .srt.
    Raises SubtitleError/UntimedSubtitleError from preparation, or
    ExtractionError if FFmpeg cannot produce the target.
    """
    from bot.services.subtitles import prepare_subtitle_file
    from bot.utils.filenames import sanitize_filename

    if target not in ("srt", "ass", "vtt"):
        raise ExtractionError(f"Unsupported target subtitle format: {target}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_subtitle_file(raw_path, out_dir, fps=fps)

    out = out_dir / sanitize_filename(f"{output_stem}.{target}")
    if prepared.format.value == target:
        Path(prepared.path).replace(out)
        return str(out)
    args = [ffmpeg, "-hide_banner", "-y", "-i", prepared.path,
            "-c:s", {"srt": "srt", "ass": "ass", "vtt": "webvtt"}[target],
            str(out)]
    code, _, err = await run_process(args, timeout=120, cancel_token=cancel_token)
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        raise ExtractionError(
            f"Subtitle conversion to {target.upper()} failed: "
            f"{tail_text(err, 300)}"
        )
    return str(out)


# ISO 639 language tag → (short code used in file names, display name, flag).
_LANGS: dict[str, tuple[str, str, str]] = {
    "eng": ("En", "English", "🇬🇧"), "fre": ("Fr", "French", "🇫🇷"),
    "ger": ("De", "German", "🇩🇪"), "ita": ("It", "Italian", "🇮🇹"),
    "spa": ("Es", "Spanish", "🇪🇸"), "por": ("Pt", "Portuguese", "🇵🇹"),
    "ara": ("Ar", "Arabic", "🇸🇦"), "rus": ("Ru", "Russian", "🇷🇺"),
    "jpn": ("Ja", "Japanese", "🇯🇵"), "chi": ("Zh", "Chinese", "🇨🇳"),
    "kor": ("Ko", "Korean", "🇰🇷"), "tur": ("Tr", "Turkish", "🇹🇷"),
    "pol": ("Pl", "Polish", "🇵🇱"), "dut": ("Nl", "Dutch", "🇳🇱"),
    "swe": ("Sv", "Swedish", "🇸🇪"), "nor": ("No", "Norwegian", "🇳🇴"),
    "dan": ("Da", "Danish", "🇩🇰"), "fin": ("Fi", "Finnish", "🇫🇮"),
    "cze": ("Cs", "Czech", "🇨🇿"), "gre": ("El", "Greek", "🇬🇷"),
    "heb": ("He", "Hebrew", "🇮🇱"), "hin": ("Hi", "Hindi", "🇮🇳"),
    "tha": ("Th", "Thai", "🇹🇭"), "vie": ("Vi", "Vietnamese", "🇻🇳"),
    "ind": ("Id", "Indonesian", "🇮🇩"), "may": ("Ms", "Malay", "🇲🇾"),
    "ukr": ("Uk", "Ukrainian", "🇺🇦"), "rum": ("Ro", "Romanian", "🇷🇴"),
    "hun": ("Hu", "Hungarian", "🇭🇺"), "bul": ("Bg", "Bulgarian", "🇧🇬"),
}
_LANG_ALIASES = {
    "fra": "fre", "deu": "ger", "zho": "chi", "nld": "dut", "ces": "cze",
    "ell": "gre", "ron": "rum", "msa": "may", "nob": "nor",
    "en": "eng", "fr": "fre", "de": "ger", "it": "ita", "es": "spa",
    "pt": "por", "ar": "ara", "ru": "rus", "ja": "jpn", "zh": "chi",
    "ko": "kor", "tr": "tur", "pl": "pol", "nl": "dut", "sv": "swe",
    "no": "nor", "da": "dan", "fi": "fin", "cs": "cze", "el": "gre",
    "he": "heb", "hi": "hin", "th": "tha", "vi": "vie", "id": "ind",
    "ms": "may", "uk": "ukr", "ro": "rum", "hu": "hun", "bg": "bul",
}


def subtitle_display(language: str, title: str = "") -> tuple[str, str, str]:
    """(short code, language name, flag) for a subtitle stream.

    ``("Fr", "French", "🇫🇷")`` for ``"fre"``. Unknown/undetermined tracks get
    a neutral 🌐 flag instead of a wrong guess.
    """
    key = (language or "").strip().lower()
    key = _LANG_ALIASES.get(key, key)
    if key in _LANGS:
        code, name, flag = _LANGS[key]
        if key == "por" and "br" in (title or "").lower():
            flag = "🇧🇷"   # Brazilian Portuguese, as labelled by the release
        return code, name, flag
    if key in ("", "und", "unk", "zxx", "mis", "mul"):
        return "Und", "Unknown language", "🌐"
    return key[:3].capitalize(), key.upper(), "🌐"


def select_subtitle_streams(streams: list[SubtitleStream],
                            wanted: object = None) -> list[SubtitleStream]:
    """Text subtitle streams to extract.

    ``wanted`` is the user's pick stored in ``job.options["extract_streams"]``:
    ``None``/``"all"``/empty → every text stream; a list of ``type_index``
    values → only those. Bitmap streams (PGS/VobSub) are always skipped.
    """
    text = [s for s in streams if not s.is_bitmap]
    if not isinstance(wanted, (list, tuple, set)) or not wanted:
        return text
    chosen: set[int] = set()
    for w in wanted:
        try:
            chosen.add(int(w))
        except (TypeError, ValueError):
            continue
    return [s for s in text if s.type_index in chosen]


def describe_subtitle_streams(media: MediaInfo) -> list[str]:
    """Human-readable one-liners for the embedded-subtitle picker."""
    lines = []
    for s in media.subtitles:
        flags = []
        if s.default:
            flags.append("default")
        if s.forced:
            flags.append("forced")
        if s.is_bitmap:
            flags.append("bitmap — no hardsub")
        desc = f"#{s.type_index} {s.language} {s.codec}"
        if s.title:
            desc += f" — {s.title[:30]}"
        if flags:
            desc += f" [{', '.join(flags)}]"
        lines.append(desc)
    return lines
