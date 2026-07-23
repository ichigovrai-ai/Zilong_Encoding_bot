"""Target-size bitrate math and options→EncodePlan planning."""
from __future__ import annotations

import pytest

from bot.services.encoding import calc_video_bitrate_kbps, estimate_size_bytes
from bot.services.planner import plan_from_options
from bot.utils.validation import ValidationError
from tests.conftest import make_media


def test_bitrate_for_target_size():
    # 250 MB, 20 min, 128k audio
    kbps = calc_video_bitrate_kbps(250 * 1024**2, 1200, 128)
    assert 1400 < kbps < 1650
    est = estimate_size_bytes(kbps, 128, 1200)
    assert est <= 260 * 1024**2  # lands at or under target with margin


def test_impossible_target_rejected():
    with pytest.raises(ValidationError):
        calc_video_bitrate_kbps(5 * 1024**2, 7200, 128)  # 5MB for 2 hours


def test_unknown_duration_rejected():
    with pytest.raises(ValidationError):
        calc_video_bitrate_kbps(100 * 1024**2, 0, 128)


def test_plan_basic_720p(media_1080):
    r = plan_from_options({"resolution": 720, "codec": "x264",
                           "quality": "bal"}, media_1080)
    assert (r.plan.width, r.plan.height) == (1280, 720)
    assert r.plan.video_codec == "libx264"
    assert r.plan.mode == "crf" and r.plan.crf == 23
    assert r.plan.container == "mp4" and r.plan.faststart


def test_plan_portrait(media_portrait):
    r = plan_from_options({"resolution": 720, "codec": "x265"},
                          media_portrait)
    assert (r.plan.width, r.plan.height) == (720, 1280)
    assert r.plan.container == "mkv"


def test_plan_refuses_silent_upscale(media_1080):
    r = plan_from_options({"resolution": 2160, "codec": "x264"}, media_1080)
    assert r.plan.width is None
    assert any("exceeds" in w for w in r.warnings)


def test_plan_upscale_when_confirmed(media_1080):
    r = plan_from_options({"resolution": 2160, "codec": "x264",
                           "allow_upscale": True}, media_1080)
    assert (r.plan.width, r.plan.height) == (3840, 2160)


def test_plan_target_size(media_1080):
    r = plan_from_options({"codec": "x264", "quality": "size",
                           "target_size_mb": 250}, media_1080)
    assert r.plan.mode == "size" and r.plan.two_pass
    assert r.plan.video_bitrate_kbps > 0


def test_plan_quality_shift(media_1080):
    small = plan_from_options({"codec": "x264", "quality": "small"}, media_1080)
    high = plan_from_options({"codec": "x264", "quality": "high"}, media_1080)
    assert small.plan.crf > high.plan.crf


def test_plan_custom_crf(media_1080):
    r = plan_from_options({"codec": "x265", "quality": "crf", "crf": 18},
                          media_1080)
    assert r.plan.crf == 18


def test_plan_copy_rejects_resize(media_1080):
    with pytest.raises(ValidationError):
        plan_from_options({"codec": "copy", "resolution": 720}, media_1080)


def test_plan_missing_encoder_rejected(media_1080):
    with pytest.raises(ValidationError):
        plan_from_options({"codec": "av1"}, media_1080,
                          capabilities={"libsvtav1": False})


def test_plan_audio_copy_uses_probed_bitrate(media_1080):
    r = plan_from_options({"codec": "x264", "audio": "copy"}, media_1080)
    assert r.plan.audio_codec == "copy"
    assert r.plan.audio_bitrate_kbps == 128


def test_plan_no_video_stream():
    media = make_media()
    media.video = []
    with pytest.raises(ValidationError):
        plan_from_options({}, media)
