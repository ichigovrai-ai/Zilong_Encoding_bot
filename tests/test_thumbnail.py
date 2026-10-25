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


# ── random frame selection ────────────────────────────────────────────────
import random

from bot.services import thumbnail as thumb_mod
from bot.services.thumbnail import fixed_seek, pick_seek


def test_pick_seek_stays_inside_the_safe_window():
    rng = random.Random(1)
    for dur in (9.0, 30.0, 600.0, 1380.0, 7200.0):
        margin = max(6.0, dur * 0.04)
        lo = max(3.0, dur * 0.04)
        for _ in range(300):
            s = pick_seek(dur, rng)
            assert lo <= s <= max(lo, dur - margin), (dur, s)


def test_pick_seek_covers_the_whole_video_not_just_one_spot():
    rng = random.Random(7)
    seeks = [pick_seek(1380.0, rng) for _ in range(400)]    # a 23-minute episode
    assert min(seeks) < 1380 * 0.15 and max(seeks) > 1380 * 0.80
    assert len(set(seeks)) > 300                            # genuinely varied


def test_pick_seek_short_video_uses_the_start():
    assert pick_seek(5.0) == 0.0 and pick_seek(8.0) == 0.0


def test_fixed_seek_is_the_old_behaviour():
    assert fixed_seek(1000.0) == 120.0 and fixed_seek(10.0) == 3.0
    assert fixed_seek(5.0) == 0.0


class _FakeRun:
    """Stands in for run_process: records -ss, optionally fails first call."""
    def __init__(self, fail_first: bool = False):
        self.seeks: list[float] = []
        self.fail_first = fail_first

    async def __call__(self, args, timeout=0, cancel_token=None):
        out = Path(args[-1])
        if "-ss" in args:
            self.seeks.append(float(args[args.index("-ss") + 1]))
        if self.fail_first and len(self.seeks) == 1 and out.name == "cover.jpg":
            return 1, "", "boom"
        out.write_bytes(b"\xff\xd8" + b"0" * 1000)
        return 0, "", ""


async def test_make_thumbnails_uses_a_random_seek(tmp_path, monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(thumb_mod, "run_process", fake)
    a = await make_thumbnails("v.mkv", tmp_path, 1380.0, rng=random.Random(1))
    b = await make_thumbnails("v.mkv", tmp_path, 1380.0, rng=random.Random(2))
    assert a.cover and b.cover
    assert fake.seeks[0] != fake.seeks[1]                    # not always the same frame
    assert fake.seeks[0] == pick_seek(1380.0, random.Random(1))


async def test_randomize_false_keeps_the_fixed_position(tmp_path, monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(thumb_mod, "run_process", fake)
    await make_thumbnails("v.mkv", tmp_path, 1000.0, randomize=False)
    assert fake.seeks[0] == 120.0


async def test_falls_back_to_fixed_seek_when_random_one_fails(tmp_path, monkeypatch):
    fake = _FakeRun(fail_first=True)
    monkeypatch.setattr(thumb_mod, "run_process", fake)
    ts = await make_thumbnails("v.mkv", tmp_path, 1000.0, rng=random.Random(3))
    assert ts.cover and ts.thumb                              # preview not lost
    assert fake.seeks[0] != 120.0 and fake.seeks[1] == 120.0  # random, then fixed


@needs_ffmpeg
async def test_real_ffmpeg_frames_differ_between_calls(tmp_path: Path):
    src = tmp_path / "long.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=12:duration=60",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True,
    )
    covers = []
    for seed in (11, 22, 33):
        out = tmp_path / f"o{seed}"
        ts = await make_thumbnails(str(src), out, 60.0, rng=random.Random(seed))
        assert ts.cover and ts.thumb
        covers.append(Path(ts.cover).read_bytes())
    assert len(set(covers)) >= 2          # the testsrc2 counter differs per moment


# ── the HD cover must survive a video that is shorter than its duration ───
from bot.services.thumbnail import seek_chain


def test_seek_chain_starts_random_then_gets_safer():
    chain = seek_chain(1532.5, rng=random.Random(1))
    assert chain[0] == pick_seek(1532.5, random.Random(1))
    assert chain[1:] == [fixed_seek(1532.5), round(1532.5 * 0.05, 2), 3.0, 0.0]
    assert len(set(chain)) == len(chain)


def test_seek_chain_without_random_and_for_short_or_unknown_duration():
    assert seek_chain(1000.0, randomize=False)[0] == 120.0
    assert 0.0 in seek_chain(5.0) and seek_chain(5.0)[0] == 0.0   # short: start first
    assert 0.0 in seek_chain(1532.5) and seek_chain(1532.5)[-1] == 0.0
    assert seek_chain(0.0)[0] == 0.0                              # unknown length
    for dur in (0.0, 5.0, 20.0, 1532.5):
        for s in seek_chain(dur, rng=random.Random(4)):
            assert s >= 0 and (dur == 0 or s == 0 or s < dur)


async def test_chain_keeps_going_until_a_position_works(tmp_path, monkeypatch):
    class _FailFirst(_FakeRun):
        async def __call__(self, args, timeout=0, cancel_token=None):
            out = Path(args[-1])
            if "-ss" in args:
                self.seeks.append(float(args[args.index("-ss") + 1]))
            if out.name == "cover.jpg" and len(self.seeks) <= 3:
                return 1, "", "Nothing was written into output file"
            out.write_bytes(b"\xff\xd8" + b"0" * 1000)
            return 0, "", ""

    fake = _FailFirst()
    monkeypatch.setattr(thumb_mod, "run_process", fake)
    ts = await make_thumbnails("v.mkv", tmp_path, 1532.5, rng=random.Random(2))
    assert ts.cover and ts.thumb                       # HD cover not lost
    assert len(fake.seeks) == 4                        # 3 failures, 4th worked
    assert fake.seeks[-1] == 3.0


async def test_all_positions_failing_falls_back_to_the_small_thumbnail(
        tmp_path, monkeypatch, caplog):
    class _CoverNeverWorks(_FakeRun):
        async def __call__(self, args, timeout=0, cancel_token=None):
            out = Path(args[-1])
            if "-ss" in args:
                self.seeks.append(float(args[args.index("-ss") + 1]))
            if out.name == "cover.jpg":
                return 1, "", "no packets"
            out.write_bytes(b"\xff\xd8" + b"0" * 1000)
            return 0, "", ""

    import logging
    monkeypatch.setattr(thumb_mod, "run_process", _CoverNeverWorks())
    with caplog.at_level(logging.WARNING, logger="bot.services.thumbnail"):
        ts = await make_thumbnails("v.mkv", tmp_path, 1000.0, rng=random.Random(5))
    assert ts.cover is None and ts.thumb               # small thumb still made
    assert "No HD cover" in caplog.text


@needs_ffmpeg
async def test_real_ffmpeg_audio_longer_than_video_still_gets_an_hd_cover(
        tmp_path: Path):
    # 30 s of video but 120 s of audio: the container claims 120 s, so random /
    # 12% positions can land past the end of the picture.
    src = tmp_path / "audio_longer.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=120",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)],
        check=True,
    )
    for seed in range(6):                               # several random draws
        ts = await make_thumbnails(str(src), tmp_path / f"o{seed}", 120.0,
                                   rng=random.Random(seed))
        assert ts.cover and Path(ts.cover).stat().st_size > 5_000, seed
        assert ts.thumb
