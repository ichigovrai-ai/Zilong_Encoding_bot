"""Minimal .torrent parsing: bencode decode + magnet construction.

Only what's needed to turn an uploaded .torrent file into a magnet link for
Seedr (info-hash + display name + trackers). Stdlib only.
"""
from __future__ import annotations

import hashlib
from urllib.parse import quote


class TorrentParseError(ValueError):
    pass


def _decode(data: bytes, pos: int) -> tuple[object, int]:
    ch = data[pos:pos + 1]
    if ch == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if ch == b"l":
        pos += 1
        items = []
        while data[pos:pos + 1] != b"e":
            item, pos = _decode(data, pos)
            items.append(item)
        return items, pos + 1
    if ch == b"d":
        pos += 1
        out: dict[bytes, object] = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _decode(data, pos)
            val, pos = _decode(data, pos)
            if not isinstance(key, bytes):
                raise TorrentParseError("Non-string dict key")
            out[key] = val
        return out, pos + 1
    if ch.isdigit():
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start = colon + 1
        return data[start:start + length], start + length
    raise TorrentParseError(f"Invalid bencode at byte {pos}")


def _encode(obj: object) -> bytes:
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, list):
        return b"l" + b"".join(_encode(i) for i in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(
            _encode(k) + _encode(v) for k, v in sorted(obj.items())
        ) + b"e"
    raise TorrentParseError(f"Cannot bencode {type(obj)}")


def torrent_to_magnet(data: bytes) -> str:
    """Build a magnet link from raw .torrent bytes."""
    try:
        decoded, _ = _decode(data, 0)
    except (ValueError, IndexError) as exc:
        raise TorrentParseError(f"Not a valid .torrent file: {exc}") from exc
    if not isinstance(decoded, dict) or b"info" not in decoded:
        raise TorrentParseError("The .torrent file has no info dictionary.")
    info = decoded[b"info"]
    infohash = hashlib.sha1(_encode(info)).hexdigest()

    magnet = f"magnet:?xt=urn:btih:{infohash}"
    name = info.get(b"name") if isinstance(info, dict) else None
    if isinstance(name, bytes):
        magnet += "&dn=" + quote(name.decode("utf-8", errors="replace"))

    trackers: list[bytes] = []
    announce = decoded.get(b"announce")
    if isinstance(announce, bytes):
        trackers.append(announce)
    for tier in decoded.get(b"announce-list") or []:
        if isinstance(tier, list):
            trackers.extend(t for t in tier if isinstance(t, bytes))
    for tracker in trackers[:8]:
        magnet += "&tr=" + quote(tracker.decode("utf-8", errors="replace"))
    return magnet
