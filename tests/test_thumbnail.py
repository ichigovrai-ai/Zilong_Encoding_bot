"""HD thumbnail generation: HDR detection + real cover/thumb output."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bot.services.ffprobe import probe_media
from bot.services.thumbnail import ThumbSet, _is_hdr, make_thumbnails
from tests.conftest import make_media

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def test_is_hdr_detects_10bit():
    m = make_media()
    m.video[0].pix_fmt = "yuv420p10le"
    assert _is_hdr(m)


def test_is_hdr_false_for_8bit():
    m = make_media()
    m.video[0].pix_fmt = "yuv420p"
    assert not _is_hdr(m)
    assert not _is_hdr(None)


@needs_ffmpeg
async def test_make_thumbnails_produces_cover_and_thumb(tmp_path: Path):
    src = tmp_path / "src.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True,
    )
    ts = await make_thumbnails(str(src), tmp_path, 6.0)
    assert ts.cover and ts.thumb

    cover = await probe_media(ts.cover)
    assert cover.main_video.width == 1280        # HD poster keeps full width

    thumb = await probe_media(ts.thumb)
    assert thumb.main_video.width == 320          # Telegram-compliant
    assert Path(ts.thumb).stat().st_size < 200_000


@needs_ffmpeg
async def test_no_upscale_small_source(tmp_path: Path):
    src = tmp_path / "small.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True,
    )
    ts = await make_thumbnails(str(src), tmp_path, 6.0)
    cover = await probe_media(ts.cover)
    assert cover.main_video.width == 640          # not upscaled to 1280


async def test_missing_file_returns_empty(tmp_path: Path):
    ts = await make_thumbnails(str(tmp_path / "nope.mkv"), tmp_path, 6.0)
    assert isinstance(ts, ThumbSet)
    assert ts.cover is None and ts.thumb is None
