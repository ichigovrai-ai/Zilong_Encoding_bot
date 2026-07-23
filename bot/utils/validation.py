"""Input validation: magnets, URLs, numeric encode settings."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qs, urlparse

_BTIH_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$|^[A-Za-z2-7]{32}$")


class ValidationError(ValueError):
    """User-facing validation failure — message is safe to show in chat."""


def validate_magnet(magnet: str) -> str:
    """Validate a magnet link; returns the info-hash (lowercased hex/base32)."""
    magnet = (magnet or "").strip()
    if not magnet.lower().startswith("magnet:?"):
        raise ValidationError("Not a magnet link (must start with magnet:?).")
    qs = parse_qs(urlparse(magnet).query)
    for xt in qs.get("xt", []):
        if xt.lower().startswith("urn:btih:") or xt.lower().startswith("urn:btmh:"):
            infohash = xt.split(":")[-1]
            if _BTIH_RE.match(infohash):
                return infohash.lower()
    raise ValidationError("Magnet link has no valid BitTorrent info-hash (xt=urn:btih:…).")


MAGNET_IN_TEXT = re.compile(r"magnet:\?[^\s\"'<>]+")


def _is_private_host(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    host = host.lower().rstrip(".")
    return host in ("localhost",) or host.endswith(".local") or host.endswith(".internal")


def validate_download_url(url: str) -> str:
    """Allow only http/https URLs to public hosts. Blocks file://, localhost,
    private/link-local IPs, and userinfo tricks (user:pass@evil)."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError("Only http:// and https:// links are supported.")
    if not parsed.hostname:
        raise ValidationError("The link has no host.")
    if parsed.username or parsed.password:
        raise ValidationError("Links with embedded credentials are not accepted.")
    if _is_private_host(parsed.hostname):
        raise ValidationError("Links to local or private addresses are not allowed.")
    return url


def validate_crf(value: str | int, codec: str = "libx264") -> int:
    try:
        crf = int(value)
    except (TypeError, ValueError):
        raise ValidationError("CRF must be a whole number.") from None
    hi = 63 if "av1" in codec else 51
    if not 0 <= crf <= hi:
        raise ValidationError(f"CRF must be between 0 and {hi} for this codec.")
    return crf


def validate_target_size_mb(value: str | float) -> int:
    try:
        mb = float(str(value).lower().replace("mb", "").strip())
    except ValueError:
        raise ValidationError("Target size must be a number of megabytes, e.g. 250.") from None
    if not 5 <= mb <= 64 * 1024:
        raise ValidationError("Target size must be between 5 MB and 64 GB.")
    return int(mb)


def validate_custom_resolution(text: str) -> tuple[int, int] | int:
    """Accept '1280x720' (exact) or '720' (short-side target)."""
    text = (text or "").strip().lower().replace("×", "x")
    if m := re.fullmatch(r"(\d{2,5})\s*x\s*(\d{2,5})", text):
        w, h = int(m.group(1)), int(m.group(2))
        if not (16 <= w <= 7680 and 16 <= h <= 4320):
            raise ValidationError("Resolution out of range (16x16 … 7680x4320).")
        return (w - w % 2, h - h % 2)
    if m := re.fullmatch(r"(\d{2,4})p?", text):
        h = int(m.group(1))
        if not 16 <= h <= 4320:
            raise ValidationError("Height must be between 16 and 4320.")
        return h - h % 2
    raise ValidationError("Send a resolution like 1280x720 or 720.")


def validate_bitrate_kbps(value: str | int, lo: int = 16, hi: int = 100_000) -> int:
    raw = str(value).lower().replace("kbps", "").replace("k", "").strip()
    try:
        kbps = int(raw)
    except ValueError:
        raise ValidationError("Bitrate must be a number in kbps, e.g. 2500.") from None
    if not lo <= kbps <= hi:
        raise ValidationError(f"Bitrate must be between {lo} and {hi} kbps.")
    return kbps
