from __future__ import annotations

import pytest

from bot.services.ffprobe import AudioStream, MediaInfo, VideoStream


def make_media(width: int = 1920, height: int = 1080, duration: float = 1200.0,
               audio_kbps: int = 128) -> MediaInfo:
    return MediaInfo(
        path="input.mkv", container="matroska", duration=duration,
        size=500 * 1024**2, bitrate=3_000_000,
        video=[VideoStream(index=0, codec="h264", width=width, height=height,
                           fps=23.976, pix_fmt="yuv420p", bitrate=2_500_000)],
        audio=[AudioStream(index=1, codec="aac", channels=2, language="jpn",
                           title="", bitrate=audio_kbps * 1000)],
    )


@pytest.fixture
def media_1080():
    return make_media()


@pytest.fixture
def media_portrait():
    return make_media(width=1080, height=1920)
