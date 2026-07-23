"""Human-readable sizes and durations."""
from __future__ import annotations


def human_size(n: float | int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_hms(secs: float | int) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_timestamp_ms(text: str) -> int | None:
    """Parse 'HH:MM:SS,mmm' / 'HH:MM:SS.mmm' / 'M:SS.cc' into milliseconds."""
    text = text.strip().replace(",", ".")
    parts = text.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        sec = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[-3]) if len(parts) == 3 else 0
    except ValueError:
        return None
    if sec < 0 or minutes < 0 or hours < 0:
        return None
    return int(round((hours * 3600 + minutes * 60 + sec) * 1000))
