"""FFmpeg argument generation: no shell strings, correct filters, safe paths."""
from __future__ import annotations

from bot.services.encoding import EncodePlan
from bot.services.ffmpeg import (
    build_encode_args, build_video_filter, escape_filter_path,
)


def _plan(**kw) -> EncodePlan:
    return EncodePlan(**kw)


def test_basic_crf_args():
    args = build_encode_args(_plan(video_codec="libx264", crf=23),
                             "input.mkv", "out.mp4")
    assert args[0] == "ffmpeg"
    assert "-c:v" in args and args[args.index("-c:v") + 1] == "libx264"
    assert "-crf" in args and args[args.index("-crf") + 1] == "23"
    assert "-movflags" in args and "+faststart" in args
    assert "out.mp4" == args[-1]
    assert all(isinstance(a, str) for a in args)


def test_scale_filter_included():
    plan = _plan(width=1280, height=720)
    vf = build_video_filter(plan)
    assert "scale=1280:720" in vf
    assert vf.index("scale") < vf.index("format")


def test_hardsub_filter_order_scale_before_subtitles():
    plan = _plan(width=1280, height=720, subtitle_mode="hard",
                 subtitle_path="subtitles/sub.ass", fonts_dir="fonts")
    vf = build_video_filter(plan)
    assert vf.index("scale=") < vf.index("subtitles=")
    assert "fontsdir=" in vf


def test_embedded_hardsub_uses_si():
    plan = _plan(subtitle_mode="hard", subtitle_stream=2)
    vf = build_video_filter(plan, source_path="input.mkv")
    assert "si=2" in vf


def test_filter_path_escaping():
    escaped = escape_filter_path(r"C:\work\it's [here].ass")
    assert "\\:" in escaped and "\\'" in escaped and "\\[" in escaped
    assert "\\\\" not in escaped.replace("\\\\", "")  # backslashes normalised


def test_two_pass_size_mode():
    plan = _plan(video_codec="libx264", mode="size",
                 video_bitrate_kbps=1500, two_pass=True)
    p1 = build_encode_args(plan, "in.mkv", "out.mp4", pass_num=1)
    p2 = build_encode_args(plan, "in.mkv", "out.mp4", pass_num=2)
    assert "-pass" in p1 and p1[p1.index("-pass") + 1] == "1"
    assert "-f" in p1 and "null" in p1        # first pass discards output
    assert "-an" in p1
    assert "-pass" in p2 and p2[p2.index("-pass") + 1] == "2"
    assert p2[-1] == "out.mp4"
    assert "-b:v" in p2 and p2[p2.index("-b:v") + 1] == "1500k"


def test_x265_two_pass_uses_params():
    plan = _plan(video_codec="libx265", mode="size",
                 video_bitrate_kbps=1000, two_pass=True)
    p1 = build_encode_args(plan, "in.mkv", "out.mkv", pass_num=1)
    joined = " ".join(p1)
    assert "-x265-params" in p1 and "pass=1" in joined


def test_copy_codec_no_filters():
    plan = _plan(video_codec="copy", mode="copy")
    args = build_encode_args(plan, "in.mkv", "out.mkv")
    assert "copy" in args
    assert "-vf" not in args and "-crf" not in args


def test_audio_copy_and_encode():
    a1 = build_encode_args(_plan(audio_codec="copy"), "i.mkv", "o.mkv")
    assert a1[a1.index("-c:a") + 1] == "copy"
    a2 = build_encode_args(_plan(audio_codec="aac", audio_bitrate_kbps=160),
                           "i.mkv", "o.mp4")
    assert a2[a2.index("-c:a") + 1] == "aac"
    assert a2[a2.index("-b:a") + 1] == "160k"


def test_soft_subs_second_input():
    plan = _plan(subtitle_mode="soft", subtitle_path="subtitles/sub.srt",
                 container="mkv")
    args = build_encode_args(plan, "in.mkv", "out.mkv",
                             extra_inputs=["subtitles/sub.srt"])
    assert args.count("-i") == 2
    assert "-map" in args and "1:s:0?" in args
    assert "-c:s" in args


def test_hevc_gets_hvc1_tag():
    args = build_encode_args(_plan(video_codec="libx265"), "i.mkv", "o.mp4")
    assert "-tag:v" in args and "hvc1" in args


def test_progress_pipe_always_present():
    args = build_encode_args(_plan(), "i.mkv", "o.mp4")
    assert "-progress" in args and "pipe:1" in args
