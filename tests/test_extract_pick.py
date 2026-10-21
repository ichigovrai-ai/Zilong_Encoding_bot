"""Choosing which embedded subtitle(s) to extract."""
from __future__ import annotations

from bot.services.extraction import select_subtitle_streams
from bot.services.ffprobe import SubtitleStream
from bot.ui import keyboards
from bot.ui.callbacks import parse


def _sub(i: int, lang: str, codec: str = "ass") -> SubtitleStream:
    return SubtitleStream(index=i + 1, type_index=i, codec=codec, language=lang,
                          title="", default=False, forced=False)


STREAMS = [_sub(0, "eng"), _sub(1, "fre"), _sub(2, "ger"),
           _sub(3, "ita", "hdmv_pgs_subtitle")]


def test_default_extracts_all_text_streams():
    for wanted in (None, "all", [], ()):
        assert [s.type_index for s in select_subtitle_streams(STREAMS, wanted)] == [0, 1, 2]


def test_pick_one_stream():
    assert [s.language for s in select_subtitle_streams(STREAMS, [1])] == ["fre"]


def test_pick_ignores_bitmap_and_unknown():
    assert select_subtitle_streams(STREAMS, [3]) == []   # bitmap never extracted
    assert select_subtitle_streams(STREAMS, [99]) == []
    assert [s.type_index for s in select_subtitle_streams(STREAMS, ["2", "x"])] == [2]


def test_menu_lists_text_streams_and_all_button():
    probe_subs = [{"type_index": s.type_index, "language": s.language,
                   "codec": s.codec, "title": "", "bitmap": s.is_bitmap}
                  for s in STREAMS]
    kb = keyboards.extract_subs_menu("abcd1234", probe_subs)
    buttons = [b for row in kb.inline_keyboard for b in row]
    args = [parse(b.callback_data).arg for b in buttons]
    assert args[0] == "exsall"
    assert {"exs0", "exs1", "exs2"} <= set(args)
    assert "exs3" not in args                      # bitmap stream not offered
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


# ── readable names for extracted subtitles ────────────────────────────────
from bot.services.extraction import subtitle_display


def test_subtitle_display_known_languages():
    assert subtitle_display("fre") == ("Fr", "French", "🇫🇷")
    assert subtitle_display("fra")[0] == "Fr"            # ISO 639-2/T alias
    assert subtitle_display("en")[0] == "En"             # 2-letter tag
    assert subtitle_display("eng")[2] == "🇬🇧"
    assert subtitle_display("por", "Português (BR)")[2] == "🇧🇷"
    assert subtitle_display("por")[2] == "🇵🇹"


def test_subtitle_display_unknown_is_neutral():
    assert subtitle_display("und") == ("Und", "Unknown language", "🌐")
    assert subtitle_display("")[0] == "Und"
    code, name, flag = subtitle_display("xyz")
    assert (code, name, flag) == ("Xyz", "XYZ", "🌐")
