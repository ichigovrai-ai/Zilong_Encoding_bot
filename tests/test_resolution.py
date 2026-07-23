"""Resolution math: aspect preservation, portrait/square handling, upscale
guards, even dimensions, letterboxing."""
from __future__ import annotations

import pytest

from bot.services.encoding import (
    compute_dimensions, compute_exact_dimensions, even,
)
from bot.utils.validation import ValidationError


def test_landscape_16_9_targets():
    d = compute_dimensions(1920, 1080, 720)
    assert (d.width, d.height) == (1280, 720)
    d = compute_dimensions(1920, 1080, 360)
    assert (d.width, d.height) == (640, 360)


def test_portrait_targets_shorter_side():
    d = compute_dimensions(1080, 1920, 720)
    assert (d.width, d.height) == (720, 1280)


def test_square_video():
    d = compute_dimensions(1000, 1000, 480)
    assert (d.width, d.height) == (480, 480)


def test_non_16_9_aspect_preserved():
    d = compute_dimensions(1920, 800, 480)  # 2.4:1 scope
    assert d.height == 480
    assert abs(d.width / d.height - 1920 / 800) < 0.01
    assert d.width % 2 == 0 and d.height % 2 == 0


def test_odd_dimensions_become_even():
    d = compute_dimensions(1279, 533, 360)
    assert d.width % 2 == 0 and d.height % 2 == 0


def test_no_upscale_by_default():
    d = compute_dimensions(1280, 720, 1080)
    assert d.width is None and d.height is None
    assert d.upscale_needed


def test_upscale_when_allowed():
    d = compute_dimensions(1280, 720, 1080, allow_upscale=True)
    assert (d.width, d.height) == (1920, 1080)
    assert not d.upscale_needed


def test_same_size_keeps_original():
    d = compute_dimensions(1280, 720, 720)
    assert d.width is None and not d.upscale_needed


def test_unknown_source_raises():
    with pytest.raises(ValidationError):
        compute_dimensions(0, 0, 720)


def test_exact_fit_bounding_box():
    d = compute_exact_dimensions(1920, 1080, 1280, 1280)
    assert (d.width, d.height) == (1280, 720)
    assert d.letterbox is None


def test_exact_fit_letterbox():
    d = compute_exact_dimensions(1920, 800, 1280, 720, letterbox=True)
    assert d.letterbox == (1280, 720)
    assert d.width == 1280 and d.height < 720


def test_even_floor():
    assert even(719) == 718
    assert even(720) == 720
    assert even(1) == 2
