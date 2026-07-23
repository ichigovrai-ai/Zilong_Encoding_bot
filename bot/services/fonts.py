"""Font attachment extraction for ASS hardsubbing.

Anime MKVs embed the fonts their ASS subtitles reference. We dump all font
attachments into the job's fonts/ directory and hand that directory to the
subtitles filter (fontsdir), so styling renders as authored. Missing fonts are
reported as warnings — libass falls back to the configured fallback font.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot.services.ffprobe import MediaInfo
from bot.utils.subprocesses import CancelToken, run_process

log = logging.getLogger(__name__)


async def extract_fonts(
    input_path: str, fonts_dir: str | Path, media: MediaInfo, *,
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
) -> list[str]:
    """Dump font attachments from `input_path` into `fonts_dir`.
    Returns extracted filenames. Non-fatal on failure."""
    fonts = [a for a in media.attachments if a.is_font]
    if not fonts:
        return []
    fonts_dir = Path(fonts_dir)
    fonts_dir.mkdir(parents=True, exist_ok=True)
    # -dump_attachment:t "" dumps every attachment using its own filename;
    # cwd confines the writes to fonts_dir. ffmpeg exits non-zero ("At least
    # one output file must be specified") after dumping — that is expected.
    args = [ffmpeg, "-hide_banner", "-y", "-dump_attachment:t", "",
            "-i", str(Path(input_path).resolve())]
    try:
        await run_process(args, timeout=120, cancel_token=cancel_token,
                          cwd=str(fonts_dir))
    except asyncio.TimeoutError:
        log.warning("Font extraction timed out for %s", input_path)
    extracted = sorted(
        p.name for p in fonts_dir.iterdir()
        if p.suffix.lower() in (".ttf", ".otf", ".ttc")
    )
    log.info("Extracted %d font(s) from %s", len(extracted), Path(input_path).name)
    return extracted


def missing_font_warnings(referenced: set[str], extracted: list[str],
                          fallback: str) -> list[str]:
    """Best-effort check: filename stems vs referenced family names."""
    if not referenced:
        return []
    stems = {Path(f).stem.lower().replace("-", "").replace(" ", "") for f in extracted}
    missing = []
    for family in referenced:
        key = family.lower().replace("-", "").replace(" ", "")
        if not any(key in s or s in key for s in stems):
            missing.append(family)
    if missing:
        return [
            f"Fonts not found in attachments (system/fallback '{fallback}' will "
            f"be used): {', '.join(sorted(missing)[:6])}"
        ]
    return []
