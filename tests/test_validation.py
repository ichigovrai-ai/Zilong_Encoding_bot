from __future__ import annotations

import pytest

from bot.utils.validation import (
    ValidationError, validate_bitrate_kbps, validate_crf,
    validate_custom_resolution, validate_download_url, validate_magnet,
    validate_target_size_mb,
)

HASH40 = "a" * 40


def test_magnet_valid_hex40():
    assert validate_magnet(f"magnet:?xt=urn:btih:{HASH40}&dn=x") == HASH40


def test_magnet_valid_base32():
    b32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    assert validate_magnet(f"magnet:?xt=urn:btih:{b32}") == b32.lower()


@pytest.mark.parametrize("bad", [
    "", "http://example.com", "magnet:?dn=onlyname",
    "magnet:?xt=urn:btih:tooshort", "magnet:?xt=urn:btih:" + "z" * 40,
])
def test_magnet_invalid(bad):
    with pytest.raises(ValidationError):
        validate_magnet(bad)


def test_url_schemes():
    assert validate_download_url("https://example.com/v.mp4")
    for bad in ("file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"):
        with pytest.raises(ValidationError):
            validate_download_url(bad)


@pytest.mark.parametrize("bad", [
    "http://localhost/x.mp4", "http://127.0.0.1/x.mp4",
    "http://192.168.1.5/x.mp4", "http://10.0.0.1/v.mkv",
    "http://[::1]/x.mp4", "http://user:pass@example.com/x.mp4",
])
def test_url_private_hosts_blocked(bad):
    with pytest.raises(ValidationError):
        validate_download_url(bad)


def test_crf_bounds():
    assert validate_crf("23") == 23
    assert validate_crf(0) == 0
    with pytest.raises(ValidationError):
        validate_crf(52)
    assert validate_crf(60, codec="libsvtav1") == 60
    with pytest.raises(ValidationError):
        validate_crf("abc")


def test_target_size():
    assert validate_target_size_mb("250") == 250
    assert validate_target_size_mb("250MB") == 250
    with pytest.raises(ValidationError):
        validate_target_size_mb("1")
    with pytest.raises(ValidationError):
        validate_target_size_mb("nonsense")


def test_custom_resolution():
    assert validate_custom_resolution("1280x720") == (1280, 720)
    assert validate_custom_resolution("1281×721") == (1280, 720)
    assert validate_custom_resolution("720") == 720
    assert validate_custom_resolution("720p") == 720
    with pytest.raises(ValidationError):
        validate_custom_resolution("9999x9999")
    with pytest.raises(ValidationError):
        validate_custom_resolution("hello")


def test_bitrate():
    assert validate_bitrate_kbps("2500") == 2500
    assert validate_bitrate_kbps("2500k") == 2500
    with pytest.raises(ValidationError):
        validate_bitrate_kbps("2")
