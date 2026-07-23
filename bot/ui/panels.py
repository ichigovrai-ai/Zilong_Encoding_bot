"""Emoji progress panels — the bot's visual house style.

Reproduces and refines the panel language of Zilong_multiusage
(`services/utils.py` there): ▰▱ bars, the 🐌→🌪️ speed scale, ✅🔄⏳▫️ stage
checklists, thin separators and the ✨ footer. Everything here is a pure
function: state in → HTML string out, phone-safe (≤42 chars per line).

Three user-selectable styles (cycled in /settings):
    A "Compact" — ▰▱ bar, no footer
    B "Blocks"  — █░ bar, footer
    C "Accent"  — ━╌ bar, footer
"""
from __future__ import annotations

from html import escape

from bot.utils.timefmt import fmt_hms, human_size

SEP = "──────────────────────"
DOTS = "· · · · · · · · · · · ·"
FOOTER = "✨ <i>Time is precious — I'll handle this while you do something else.</i>"

PANEL_STYLES: tuple[str, ...] = ("A", "B", "C")
PANEL_STYLE_LABELS: dict[str, str] = {"A": "Compact", "B": "Blocks", "C": "Accent"}
_BAR_CHARS: dict[str, tuple[str, str, int]] = {
    "A": ("▰", "▱", 10),
    "B": ("█", "░", 12),
    "C": ("━", "╌", 12),
}

_KiB = 1024
_MiB = 1024 * _KiB


def norm_style(style: str | None) -> str:
    s = str(style or "A").strip().upper()[:1]
    return s if s in PANEL_STYLES else "A"


def next_style(style: str | None) -> str:
    cur = norm_style(style)
    return PANEL_STYLES[(PANEL_STYLES.index(cur) + 1) % len(PANEL_STYLES)]


def style_label(style: str | None) -> str:
    return PANEL_STYLE_LABELS[norm_style(style)]


def bar(pct: float, style: str = "A") -> str:
    full, empty, width = _BAR_CHARS[norm_style(style)]
    pct = min(max(pct, 0.0), 100.0)
    filled = round(pct / 100 * width)
    return full * filled + empty * (width - filled)


def speed_emoji(bytes_per_s: float) -> str:
    """Emoji for a transfer speed (bytes/s)."""
    if bytes_per_s <= 0:
        return "🐌"
    if bytes_per_s < 512 * _KiB:
        return "🐢"
    if bytes_per_s < 2 * _MiB:
        return "🔥"
    if bytes_per_s < 10 * _MiB:
        return "⚡"
    if bytes_per_s < 50 * _MiB:
        return "🚀"
    return "🌪️"


def encode_speed_emoji(multiplier: float) -> str:
    """Emoji for an encode speed (× realtime)."""
    if multiplier <= 0:
        return "🐌"
    if multiplier < 0.5:
        return "🐢"
    if multiplier < 1.0:
        return "🔥"
    if multiplier < 2.5:
        return "⚡"
    return "🚀"


def short_name(value: str, limit: int = 40) -> str:
    """Middle-truncate: keeps the episode number / extension at the end."""
    value = str(value or "file")
    if len(value) <= limit:
        return value
    right = max(8, (limit - 1) // 2)
    left = max(1, limit - 1 - right)
    return value[:left] + "…" + value[-right:]


STAGE_ICONS: dict[str, str] = {
    "done": "✅", "active": "🔄", "waiting": "⏳", "pending": "▫️",
    "failed": "❌", "cancelled": "🛑", "skipped": "➖",
}


def stage_checklist(stages: list[tuple[str, str]], per_row: int = 2) -> list[str]:
    """Render (label, status) pairs as phone-safe rows of `icon label` cells.

    Statuses: done | active | waiting | pending | failed | cancelled | skipped.
    """
    cells = [
        f"{STAGE_ICONS.get(status, '▫️')} {escape(short_name(label, 14))}"
        for label, status in stages if label
    ]
    return ["  ·  ".join(cells[i:i + per_row])
            for i in range(0, len(cells), per_row)]


def pipeline_stages(current: str, *, with_subtitles: bool,
                    failed: bool = False, cancelled: bool = False
                    ) -> list[tuple[str, str]]:
    """Standard job checklist: Download → Analyse → Subtitles → Encode → Upload."""
    order = ["download", "analyse"]
    labels = {"download": "Download", "analyse": "Analyse",
              "subtitles": "Subtitles", "encode": "Encode", "upload": "Upload"}
    if with_subtitles:
        order.append("subtitles")
    order += ["encode", "upload"]
    out: list[tuple[str, str]] = []
    seen_current = False
    for key in order:
        if key == current:
            state = ("failed" if failed else
                     "cancelled" if cancelled else "active")
            seen_current = True
        elif not seen_current:
            state = "done"
        else:
            state = "pending"
        out.append((labels[key], state))
    if current == "done":
        out = [(label, "done") for label, _ in out]
    return out


def seedr_stages(current: str, *, failed: bool = False) -> list[tuple[str, str]]:
    """Seedr parent checklist: Magnet → Seedr cloud → Match subs → Episodes."""
    order = [("magnet", "Magnet"), ("cloud", "Seedr cloud"),
             ("match", "Match subs"), ("episodes", "Episodes")]
    out: list[tuple[str, str]] = []
    seen = False
    for key, label in order:
        if key == current:
            out.append((label, "failed" if failed else "active"))
            seen = True
        else:
            out.append((label, "pending" if seen else "done"))
    return out


# ═════════════════════════════════════════════════════════════════════════
# High-level panels
# ═════════════════════════════════════════════════════════════════════════

def _finish(lines: list[str], style: str) -> str:
    if norm_style(style) in ("B", "C"):
        lines += ["", FOOTER]
    return "\n".join(lines)


def transfer_panel(*, title: str, icon: str, filename: str, done: int,
                   total: int, speed: float = 0.0, elapsed: float = 0.0,
                   source: str = "", stages: list[tuple[str, str]] | None = None,
                   style: str = "A") -> str:
    pct = done / total * 100 if total > 0 else 0.0
    lines = [
        f"{icon} <b>{escape(title.upper())}</b>",
        SEP,
        f"📄 <code>{escape(short_name(filename))}</code>",
    ]
    if source:
        lines.append(f"🔗 <i>{escape(source)}</i>")
    if stages:
        lines.append("")
        lines += stage_checklist(stages)
    lines.append("")
    if total > 0:
        lines.append(f"{bar(pct, style)}  <b>{pct:.1f}%</b>")
        metric = f"📦 {human_size(done)} / {human_size(total)}"
    else:
        metric = f"📦 {human_size(done)}"
    if speed > 0:
        metric += f" · {speed_emoji(speed)} {human_size(speed)}/s"
    lines.append(metric)
    tail = []
    if speed > 0 and total > done:
        tail.append(f"⏳ ETA {fmt_hms((total - done) / speed)}")
    if elapsed > 0:
        tail.append(f"⏱ {fmt_hms(elapsed)}")
    if tail:
        lines.append(" · ".join(tail))
    return _finish(lines, style)


def encode_panel(*, filename: str, settings_line: str, pct: float,
                 done_s: float, total_s: float, speed: float, fps: float,
                 out_size: int, elapsed: float, eta: float | None,
                 stages: list[tuple[str, str]] | None = None,
                 pass_label: str = "", style: str = "A") -> str:
    lines = [
        "⚙️ <b>ENCODING</b>" + (f"  <i>{escape(pass_label)}</i>" if pass_label else ""),
        SEP,
        f"📄 <code>{escape(short_name(filename))}</code>",
        f"🎯 <i>{escape(settings_line)}</i>",
    ]
    if stages:
        lines.append("")
        lines += stage_checklist(stages)
    lines += [
        "",
        f"{bar(pct, style)}  <b>{pct:.1f}%</b>",
        f"🎞 {fmt_hms(done_s)} / {fmt_hms(total_s)}"
        + (f" · {encode_speed_emoji(speed)} {speed:.2f}×" if speed > 0 else ""),
    ]
    stats = []
    if fps > 0:
        stats.append(f"🎬 {fps:.0f} fps")
    if out_size > 0:
        stats.append(f"📦 {human_size(out_size)} so far")
    if stats:
        lines.append(" · ".join(stats))
    tail = [f"⏱ {fmt_hms(elapsed)}"]
    if eta is not None:
        tail.insert(0, f"⏳ ETA {fmt_hms(eta)}")
    lines.append(" · ".join(tail))
    return _finish(lines, style)


def seedr_panel(*, torrent_name: str, pct: float | None, current: str,
                downloaded: int = 0, total: int = 0, elapsed: float = 0.0,
                note: str = "", style: str = "A") -> str:
    lines = [
        "🧲 <b>SEEDR HARDSUB</b>",
        SEP,
        f"📄 <code>{escape(short_name(torrent_name))}</code>",
        "☁️ <i>Seedr → FFmpeg ⚙️</i>",
        "",
    ]
    lines += stage_checklist(seedr_stages(current))
    if pct is not None:
        lines += ["", f"{bar(pct, style)}  <b>{pct:.0f}%</b>"]
        if total > 0:
            lines.append(f"📦 {human_size(downloaded)} / {human_size(total)}")
    if elapsed > 0:
        lines.append(f"⏱ {fmt_hms(elapsed)}")
    if note:
        lines += ["", f"💡 <i>{escape(short_name(note, 60))}</i>"]
    return _finish(lines, style)


def episodes_panel(*, folder_name: str, n_videos: int, n_subs: int,
                   n_selected: int, n_uncertain: int) -> str:
    lines = [
        "🌱 <b>TORRENT READY</b>",
        SEP,
        f"📄 <code>{escape(short_name(folder_name))}</code>",
        "",
        f"🎞 <b>{n_videos}</b> video(s) · 💬 <b>{n_subs}</b> subtitle(s)"
        f" · ☑️ <b>{n_selected}</b> selected",
    ]
    if n_uncertain:
        lines.append(f"⚠️ <b>{n_uncertain}</b> uncertain match(es) — tap "
                     f"“pick subtitle…” to fix")
    lines += [
        "",
        "✅ exact · 🟢 confident · ⚠️ uncertain · ➖ no sub",
        "",
        "Select the episodes to process:",
    ]
    return "\n".join(lines)


def done_panel(*, filename: str, out_size: int, elapsed: float,
               settings_line: str = "", job_id: str = "") -> str:
    lines = [
        "✅ <b>COMPLETED</b>",
        SEP,
        f"📄 <code>{escape(short_name(filename))}</code>",
    ]
    if settings_line:
        lines.append(f"🎯 <i>{escape(settings_line)}</i>")
    lines.append(f"📦 {human_size(out_size)} · ⏱ {fmt_hms(elapsed)}")
    if job_id:
        lines.append(f"🆔 <code>{escape(job_id)}</code>")
    return "\n".join(lines)


def error_panel(*, filename: str, error: str, job_id: str = "") -> str:
    lines = [
        "❌ <b>FAILED</b>",
        SEP,
        f"📄 <code>{escape(short_name(filename))}</code>",
        "",
        f"⚠️ {escape(error[:800])}",
    ]
    if job_id:
        lines.append(f"🆔 <code>{escape(job_id)}</code>")
    return "\n".join(lines)


def cancelled_panel(*, filename: str, job_id: str = "") -> str:
    lines = [
        "🛑 <b>CANCELLED</b>",
        SEP,
        f"📄 <code>{escape(short_name(filename))}</code>",
    ]
    if job_id:
        lines.append(f"🆔 <code>{escape(job_id)}</code>")
    return "\n".join(lines)
