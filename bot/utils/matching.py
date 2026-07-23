"""Video ↔ subtitle matching for multi-episode torrents.

Normalises release names, extracts season/episode numbers, and scores
candidate pairs. Uncertain pairs are surfaced to the user instead of being
silently accepted (`MatchConfidence.UNCERTAIN`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from pathlib import PurePosixPath

VIDEO_EXTS = frozenset({".mkv", ".mp4", ".webm", ".mov", ".avi", ".ts", ".m2ts"})
SUBTITLE_EXTS = frozenset({".ass", ".ssa", ".srt", ".vtt", ".sub", ".txt"})

_BRACKETS = re.compile(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*\)")
_TAGS = re.compile(
    r"\b(?:480|540|576|720|1080|1440|2160)p\b|\b4k\b|\buhd\b"
    r"|\b(?:x|h)\.?26[456]\b|\bhevc\b|\bavc\b|\bav1\b"
    r"|\b(?:web-?dl|webrip|bluray|bdrip|brrip|hdtv|dvdrip|remux)\b"
    r"|\b(?:aac|opus|flac|ac3|eac3|dts)\b|\b(?:10|8)-?bit\b"
    r"|\b[0-9a-f]{8}\b"  # CRC
    r"|\b(?:eng?|english|fre?|french|vostfr|jpn?|japanese|spa|esp|ger|deu|ita|por|rus|ara|multi|dual)\b",
    re.IGNORECASE,
)
_SEASON_EP = re.compile(r"\bs(\d{1,2})\s*[._\- ]?\s*e(\d{1,3})\b", re.I)
_X_EP = re.compile(r"\b(\d{1,2})x(\d{2,3})\b")
_EP_WORD = re.compile(r"\b(?:episode|ep|e)\s*[._\- ]?\s*(\d{1,3})\b", re.I)
_DASH_NUM = re.compile(r"[-_ ]\s*(\d{1,3})(?:\s*(?:v\d)?)\s*$")


@dataclass(frozen=True, slots=True)
class EpisodeKey:
    season: int | None
    episode: int


def normalize_name(name: str) -> str:
    """Lowercased, tag-free, punctuation-collapsed stem for comparisons."""
    stem = PurePosixPath(name.replace("\\", "/")).stem
    stem = _BRACKETS.sub(" ", stem)
    stem = _TAGS.sub(" ", stem)
    stem = re.sub(r"[._\-]+", " ", stem)
    stem = re.sub(r"[^\w\s]", "", stem, flags=re.UNICODE)
    return re.sub(r"\s{2,}", " ", stem).strip().casefold()


def extract_episode(name: str) -> EpisodeKey | None:
    """Extract season/episode from a raw filename (before normalisation, since
    tags like ``S01E05`` would otherwise survive but ``- 05`` needs cleanup)."""
    stem = PurePosixPath(name.replace("\\", "/")).stem
    stem_nobrackets = _BRACKETS.sub(" ", stem)
    if m := _SEASON_EP.search(stem_nobrackets):
        return EpisodeKey(int(m.group(1)), int(m.group(2)))
    if m := _X_EP.search(stem_nobrackets):
        return EpisodeKey(int(m.group(1)), int(m.group(2)))
    if m := _EP_WORD.search(stem_nobrackets):
        return EpisodeKey(None, int(m.group(1)))
    cleaned = _TAGS.sub(" ", stem_nobrackets)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if m := _DASH_NUM.search(cleaned):
        return EpisodeKey(None, int(m.group(1)))
    return None


def split_title_episode(name: str) -> tuple[str, EpisodeKey | None]:
    """Split a release filename into (title-before-the-episode, EpisodeKey).

    Everything from the episode marker onward (episode number, release group,
    tags) is dropped, so the title comes out clean:
      "[Erai-raws] Otome Kaijuu Carameliser - 03 [480p CR][MultiSub].mkv"
        → ("Otome Kaijuu Carameliser", E03)
      "Mebius Dust S01E03 VOSTFR 720p x264 -Group.mkv"
        → ("Mebius Dust", S01E03)
    """
    stem = PurePosixPath(name.replace("\\", "/")).stem
    stem = _BRACKETS.sub(" ", stem)
    for pat in (_SEASON_EP, _X_EP):
        if m := pat.search(stem):
            return stem[:m.start()], EpisodeKey(int(m.group(1)), int(m.group(2)))
    if m := _EP_WORD.search(stem):
        return stem[:m.start()], EpisodeKey(None, int(m.group(1)))
    cleaned = re.sub(r"\s{2,}", " ", _TAGS.sub(" ", stem))
    if m := _DASH_NUM.search(cleaned):
        return cleaned[:m.start()], EpisodeKey(None, int(m.group(1)))
    return stem, None


class MatchConfidence(Enum):
    EXACT = "exact"          # normalised basenames identical
    HIGH = "high"            # episode number + similar titles
    UNCERTAIN = "uncertain"  # needs user confirmation
    NONE = "none"


@dataclass(slots=True)
class SubtitleMatch:
    video: str
    subtitle: str | None
    confidence: MatchConfidence
    score: float


def _dir_of(path: str) -> str:
    return str(PurePosixPath(path.replace("\\", "/")).parent)


def score_pair(video: str, subtitle: str) -> float:
    v_norm, s_norm = normalize_name(video), normalize_name(subtitle)
    if v_norm and v_norm == s_norm:
        return 1.0
    score = SequenceMatcher(None, v_norm, s_norm).ratio() * 0.6
    v_ep, s_ep = extract_episode(video), extract_episode(subtitle)
    if v_ep and s_ep:
        if v_ep.episode == s_ep.episode and (
            v_ep.season is None or s_ep.season is None or v_ep.season == s_ep.season
        ):
            score += 0.35
        else:
            score -= 0.5  # explicit episode mismatch is disqualifying
    if _dir_of(video) == _dir_of(subtitle):
        score += 0.05
    return max(0.0, min(1.0, score))


def match_subtitles(videos: list[str], subtitles: list[str]) -> list[SubtitleMatch]:
    """Greedy best-score assignment of subtitles to videos.

    Each subtitle is used at most once. Ambiguity (two subtitles scoring the
    same for one video, or best score in the grey zone) is marked UNCERTAIN.
    """
    results: list[SubtitleMatch] = []
    available = list(subtitles)
    for video in sorted(videos):
        if not available:
            results.append(SubtitleMatch(video, None, MatchConfidence.NONE, 0.0))
            continue
        scored = sorted(
            ((score_pair(video, s), s) for s in available), reverse=True
        )
        best_score, best_sub = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= 0.999:
            conf = MatchConfidence.EXACT
        elif best_score >= 0.75 and best_score - runner_up >= 0.1:
            conf = MatchConfidence.HIGH
        elif best_score >= 0.45:
            conf = MatchConfidence.UNCERTAIN
        else:
            results.append(SubtitleMatch(video, None, MatchConfidence.NONE, best_score))
            continue
        available.remove(best_sub)
        results.append(SubtitleMatch(video, best_sub, conf, round(best_score, 3)))
    return results
