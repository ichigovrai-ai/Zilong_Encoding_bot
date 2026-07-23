"""Compact callback-data codec.

Format: ``z|<verb>|<job_id>|<arg>`` — always well under Telegram's 64-byte
limit. Callback data carries ONLY a verb + job id + one short argument; all
real state lives in the job row (options JSON).
"""
from __future__ import annotations

from dataclasses import dataclass

PREFIX = "z"

# Verbs (kept short on purpose):
#   a  = choose action            r  = resolution        c  = video codec
#   q  = quality mode             s  = subtitle mode     ss = embedded sub pick
#   u  = upscale confirmation     go = start job         bk = back one step
#   x  = cancel job               e  = toggle episode    ea = toggle all episodes
#   fx = fix episode's subtitle   fs = pick subtitle for episode
#   st = user setting toggle (no job id)
#   au = audio codec              cv = convert subtitle to format
#   rt = retry failed job         n  = noop (decorative button)
VALID_VERBS = frozenset({
    "a", "r", "c", "q", "s", "ss", "u", "go", "bk", "x",
    "e", "ea", "fx", "fs", "st", "au", "cv", "rt", "n",
})


@dataclass(frozen=True, slots=True)
class Callback:
    verb: str
    job_id: str
    arg: str = ""

    def pack(self) -> str:
        data = f"{PREFIX}|{self.verb}|{self.job_id}|{self.arg}"
        if len(data.encode()) > 64:
            raise ValueError(f"Callback data too long: {data!r}")
        return data


def pack(verb: str, job_id: str, arg: str = "") -> str:
    return Callback(verb, job_id, str(arg)).pack()


def parse(data: str) -> Callback | None:
    """Returns None for anything malformed — never raises on user input."""
    if not isinstance(data, str) or len(data) > 64:
        return None
    parts = data.split("|", 3)
    if len(parts) < 3 or parts[0] != PREFIX:
        return None
    verb, job_id = parts[1], parts[2]
    arg = parts[3] if len(parts) > 3 else ""
    if verb not in VALID_VERBS:
        return None
    if not (job_id == "" or (len(job_id) == 8 and job_id.isalnum())):
        return None
    if len(arg) > 32:
        return None
    return Callback(verb, job_id, arg)
