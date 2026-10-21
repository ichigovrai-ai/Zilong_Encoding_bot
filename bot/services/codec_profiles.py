"""
bot/services/codec_profiles.py

Tout ce qui touche au CHOIX DU CODEC vidéo (H.264 / HEVC / AV1), pour les deux
moteurs qui ré-encodent :

  - FFmpeg local  -> ffmpeg_video_args()   (arguments -c:v ... prêts à l'emploi)
  - FreeConvert   -> fc_video_options()    (options de la tâche "convert")

Les réglages viennent de current_settings() (valeurs par défaut, modifiables via configure()) :
    enc_codec       "h264" | "hevc" | "av1"
    enc_profile     "fast" | "balanced" | "small" | "best"
    enc_resize      0 (originale) | 360 | 480 | 720 | 1080
    enc_audio_kbps  96 | 128 | 192

RÈGLE D'OR : avec le codec "h264" (défaut), RIEN ne change par rapport à
l'ancien comportement — mêmes arguments ffmpeg, même payload FreeConvert.

⚠️ FreeConvert : seuls les noms d'options H.264 sont connus avec certitude
(ceux déjà utilisés dans freeconvert.py). Pour HEVC / AV1 on interroge
l'endpoint officiel  GET /v1/query/options/convert?input_format=..&output_format=mp4
qui liste les options réellement acceptées (nom + valeurs d'enum), et on
n'envoie que ce qui y figure. Si le codec n'est pas proposé -> repli H.264
(avec un warning dans les logs). Le bouton "🔎 Test FreeConvert" du menu
Encode affiche ce que le bot a compris.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

FC_API = "https://api.freeconvert.com/v1"


# ─────────────────────────────────────────────────────────────
# Tables
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Codec:
    key: str
    label: str                       # affiché dans le menu
    tag: str                         # écrit dans le nom du fichier (smart_rename)
    ffmpeg_encoders: tuple           # par ordre de préférence
    crf_offset: int                  # CRF H.264 -> CRF de ce codec (approximation)
    crf_max: int
    fc_value_guess: str              # valeur probable de l'option video_codec FC
    fc_value_needles: tuple          # sous-chaînes pour la retrouver dans l'enum FC
    fc_name_tags: tuple              # sous-chaînes dans les NOMS d'options FC
    fc_rate_guess: Optional[str]
    fc_crf_guess: Optional[str]
    fc_speed_guess: Optional[str]
    hint: str


CODECS: dict[str, Codec] = {
    "h264": Codec(
        "h264", "H.264 (AVC)", "H.264", ("libx264",), 0, 51,
        "libx264", ("264",), ("h264",),
        "video_rate_control_h264", "video_crf_h264", "video_encoding_speed_h264_265",
        "Compatible partout.",
    ),
    "hevc": Codec(
        "hevc", "H.265 (HEVC)", "H.265", ("libx265",), 5, 51,
        "libx265", ("265", "hevc"), ("h265", "hevc", "265"),
        "video_rate_control_h265", "video_crf_h265", "video_encoding_speed_h264_265",
        "Environ 30 % plus léger à qualité égale ; lit sur la plupart des téléphones.",
    ),
    "av1": Codec(
        "av1", "AV1", "AV1", ("libsvtav1", "libaom-av1"), 9, 63,
        "libaom-av1", ("av1",), ("av1",),
        "video_rate_control_av1", "video_crf_av1", None,
        "Le plus léger, mais encodage très lent et lecture limitée (iPhone ancien, TV...).",
    ),
}
DEFAULT_CODEC = "h264"

# Même contenu que QUALITY_PROFILES de freeconvert.py : (label, crf H.264, vitesse x264/x265)
PROFILES: dict[str, tuple] = {
    "fast":     ("Fast",     25, "veryfast"),
    "balanced": ("Balanced", 23, "medium"),
    "small":    ("Small",    28, "faster"),
    "best":     ("Best",     21, "slow"),
}
DEFAULT_PROFILE = "balanced"

# hauteur -> (largeur, hauteur) ; 0 = on garde la résolution d'origine
RESIZE_CHOICES: dict[int, Optional[tuple]] = {
    0: None,
    360: (640, 360),
    480: (854, 480),
    720: (1280, 720),
    1080: (1920, 1080),
}
AUDIO_CHOICES = (96, 128, 192)

DEFAULTS = {
    "enc_codec": DEFAULT_CODEC,
    "enc_profile": DEFAULT_PROFILE,
    "enc_resize": 0,
    "enc_audio_kbps": 128,
}


# ─────────────────────────────────────────────────────────────
# Réglages utilisateur
# ─────────────────────────────────────────────────────────────

def normalize_codec(key: Optional[str]) -> str:
    key = (key or DEFAULT_CODEC).strip().lower()
    aliases = {"h265": "hevc", "x265": "hevc", "h.265": "hevc", "x264": "h264", "h.264": "h264", "avc": "h264"}
    key = aliases.get(key, key)
    return key if key in CODECS else DEFAULT_CODEC


def codec_tag(key: Optional[str]) -> str:
    return CODECS[normalize_codec(key)].tag


_overrides: dict = {}


def configure(**kwargs) -> None:
    """Change les réglages d'encodage par défaut (clés de DEFAULTS : enc_codec,
    enc_profile, enc_resize, enc_audio_kbps). Sans argument : remise à zéro."""
    _overrides.clear()
    _overrides.update({k: v for k, v in kwargs.items() if k in DEFAULTS})


def current_settings() -> dict:
    """Réglages d'encodage courants (DEFAULTS + configure())."""
    def get(name):
        return _overrides.get(name, DEFAULTS[name])

    profile = str(get("enc_profile") or DEFAULT_PROFILE).lower()
    try:
        resize = int(get("enc_resize") or 0)
    except (TypeError, ValueError):
        resize = 0
    try:
        audio = int(get("enc_audio_kbps") or 128)
    except (TypeError, ValueError):
        audio = 128
    return {
        "codec": normalize_codec(get("enc_codec")),
        "profile": profile if profile in PROFILES else DEFAULT_PROFILE,
        "resize": resize if resize in RESIZE_CHOICES else 0,
        "audio_kbps": audio if audio in AUDIO_CHOICES else 128,
    }


def scaled_crf(codec: str, h264_crf: int) -> int:
    """Traduit un CRF "échelle H.264" vers l'échelle du codec choisi.

    Approximation classique : x265 ≈ x264 + 5, AV1 ≈ x264 + 9 (échelle 0-63).
    """
    meta = CODECS[normalize_codec(codec)]
    return max(0, min(meta.crf_max, int(h264_crf) + meta.crf_offset))


# ─────────────────────────────────────────────────────────────
# FFmpeg local
# ─────────────────────────────────────────────────────────────

_SVT_PRESET = {
    "ultrafast": 12, "superfast": 11, "veryfast": 10, "faster": 9, "fast": 8,
    "medium": 7, "slow": 5, "slower": 4, "veryslow": 2,
}
_AOM_CPU_USED = {
    "ultrafast": 8, "superfast": 8, "veryfast": 7, "faster": 6, "fast": 6,
    "medium": 5, "slow": 4, "slower": 3, "veryslow": 2,
}

_encoders_cache: Optional[set] = None


async def ffmpeg_encoders() -> set:
    """Encodeurs vidéo disponibles dans le ffmpeg installé (mis en cache)."""
    global _encoders_cache
    if _encoders_cache is not None:
        return _encoders_cache
    found: set = set()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        for line in out.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            # ex: " V....D libx264   libx264 H.264 / AVC ..."
            if len(parts) >= 2 and len(parts[0]) == 6 and parts[0].startswith("V"):
                found.add(parts[1])
    except FileNotFoundError:
        pass
    _encoders_cache = found
    return found


async def pick_ffmpeg_encoder(codec: str) -> str:
    key = normalize_codec(codec)
    have = await ffmpeg_encoders()
    for name in CODECS[key].ffmpeg_encoders:
        if name in have:
            return name
    if key == "h264":
        return "libx264"          # comportement historique, même si la détection a échoué
    wanted = " / ".join(CODECS[key].ffmpeg_encoders)
    raise RuntimeError(
        f"Ce ffmpeg n'a pas d'encodeur {CODECS[key].tag} ({wanted}). "
        "Choisis H.264 dans Settings → Encode, ou installe un ffmpeg plus récent."
    )


async def ffmpeg_video_args(codec: Optional[str], h264_crf: int, speed: str, output_path: str = "") -> list:
    """Arguments `-c:v ...` pour ffmpeg.

    `h264_crf` est exprimé sur l'échelle H.264 (comme dans le code existant :
    23, 28, 20...) et converti automatiquement pour HEVC / AV1.
    Pour "h264" le résultat est exactement l'ancien  -c:v libx264 -crf N -preset P.
    """
    key = normalize_codec(codec)
    encoder = await pick_ffmpeg_encoder(key)
    crf = str(scaled_crf(key, h264_crf))

    if encoder == "libx264":
        return ["-c:v", "libx264", "-crf", crf, "-preset", speed]

    if encoder == "libx265":
        args = ["-c:v", "libx265", "-crf", crf, "-preset", speed,
                "-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]
        if output_path.lower().endswith((".mp4", ".m4v", ".mov")):
            args += ["-tag:v", "hvc1"]      # nécessaire pour la lecture Apple
        return args

    if encoder == "libsvtav1":
        return ["-c:v", "libsvtav1", "-crf", crf,
                "-preset", str(_SVT_PRESET.get(speed, 8)), "-pix_fmt", "yuv420p"]

    # libaom-av1 (beaucoup plus lent que SVT-AV1)
    return ["-c:v", "libaom-av1", "-crf", crf, "-b:v", "0",
            "-cpu-used", str(_AOM_CPU_USED.get(speed, 6)), "-row-mt", "1",
            "-pix_fmt", "yuv420p"]


# ─────────────────────────────────────────────────────────────
# FreeConvert
# ─────────────────────────────────────────────────────────────

_H264_KEYS = (
    "video_codec", "video_rate_control_h264", "video_crf_h264",
    "video_encoding_speed_h264_265",
)

_OPTS_TTL_OK = 6 * 3600
_OPTS_TTL_FAIL = 120
_fc_opts_cache: dict = {}   # (in, out) -> (expire_ts, dict | None)


async def fetch_fc_video_options(
    input_format: str, output_format: str = "mp4", api_key: Optional[str] = None,
) -> Optional[dict]:
    """Options avancées acceptées par FreeConvert pour in -> out, {nom: option}.

    Renvoie None si l'API est injoignable ou répond autrement qu'attendu
    (le code retombe alors sur les noms par défaut). Résultat mis en cache.
    """
    key = ((input_format or "mkv").lower(), (output_format or "mp4").lower())
    cached = _fc_opts_cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    result: Optional[dict] = None
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(
                f"{FC_API}/query/options/convert",
                params={"input_format": key[0], "output_format": key[1]},
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw = data.get("options") if isinstance(data, dict) else data
                    if isinstance(raw, list):
                        result = {
                            o["name"]: o for o in raw
                            if isinstance(o, dict) and o.get("name")
                        }
                else:
                    log.warning("FreeConvert options: HTTP %s", resp.status)
    except Exception as exc:
        log.warning("FreeConvert options injoignables: %s", exc)

    ttl = _OPTS_TTL_OK if result else _OPTS_TTL_FAIL
    _fc_opts_cache[key] = (time.time() + ttl, result or None)
    return result or None


def _enum_values(opt: Optional[dict]) -> list:
    values = []
    for item in (opt or {}).get("enum_values") or []:
        values.append(str(item.get("value") if isinstance(item, dict) else item))
    return values


def _pick_name(known: Optional[dict], guess: Optional[str], needle_sets: list) -> Optional[str]:
    """Nom d'option FC à utiliser : le nom deviné s'il existe, sinon une
    option dont le nom contient tous les mots d'un des `needle_sets`."""
    if known is None:
        return guess
    if guess and guess in known:
        return guess
    for needles in needle_sets:
        for name in known:
            low = name.lower()
            if all(n in low for n in needles):
                return name
    return None


async def fc_video_options(
    codec: Optional[str], h264_crf: int, speed: str, *,
    input_format: str, output_format: str = "mp4", api_key: Optional[str] = None,
) -> tuple:
    """Options vidéo FreeConvert pour `codec`.

    Retourne (codec_effectif, options). Pour "h264" -> ("h264", {}) : on ne
    touche pas au payload historique. Si FreeConvert ne propose pas le codec
    demandé -> ("h264", {}) (repli), le nom du fichier suivra donc H.264.
    """
    key = normalize_codec(codec)
    if key == "h264":
        return "h264", {}

    meta = CODECS[key]
    known = await fetch_fc_video_options(input_format, output_format, api_key)

    # 1) valeur de video_codec
    value: Optional[str] = meta.fc_value_guess
    if known is not None:
        enum = _enum_values(known.get("video_codec"))
        if enum and value not in enum:
            value = next(
                (v for v in enum if any(n in v.lower() for n in meta.fc_value_needles)), None
            )
        if value is None:
            log.warning("FreeConvert ne propose pas %s pour %s->%s : repli H.264",
                        meta.tag, input_format, output_format)
            return "h264", {}

    opts: dict = {"video_codec": value}

    # 2) contrôle du débit (crf), qualité, vitesse
    tags = meta.fc_name_tags
    rate_name = _pick_name(known, meta.fc_rate_guess, [("rate_control", t) for t in tags])
    crf_name = _pick_name(known, meta.fc_crf_guess, [("crf", t) for t in tags])
    speed_sets = [("speed", t) for t in tags] + [("preset", t) for t in tags] + [("cpu", t) for t in tags]
    speed_name = _pick_name(known, meta.fc_speed_guess, speed_sets)

    if rate_name:
        enum = _enum_values((known or {}).get(rate_name))
        if not enum or "crf" in [e.lower() for e in enum]:
            opts[rate_name] = "crf"
    if crf_name:
        opts[crf_name] = scaled_crf(key, h264_crf)
    else:
        log.warning("Option CRF %s introuvable côté FreeConvert : qualité par défaut", meta.tag)
    if speed_name:
        enum = _enum_values((known or {}).get(speed_name))
        if not enum or speed in enum:
            opts[speed_name] = speed
        else:
            log.info("Vitesse %r absente de %s : valeur par défaut FreeConvert", speed, speed_name)

    return key, opts


def merge_fc_video_options(options: dict, fc_opts: dict, audio_kbps: Optional[int] = None) -> None:
    """Applique `fc_opts` (voir fc_video_options) sur le dict d'options d'une
    tâche "convert" : retire les clés H.264 puis ajoute celles du codec choisi.
    Règle aussi le débit audio AAC."""
    if fc_opts:
        for k in _H264_KEYS:
            options.pop(k, None)
        options.update(fc_opts)
    if audio_kbps and options.get("audio_codec") == "aac":
        options["audio_bitrate_aac"] = f"{int(audio_kbps)}k"


async def describe_fc_support(api_key: Optional[str] = None) -> str:
    """Rapport texte pour le bouton « 🔎 Test FreeConvert »."""
    known = await fetch_fc_video_options("mkv", "mp4", api_key)
    if known is None:
        lines = ["API options : injoignable → noms par défaut (non vérifiés)"]
    else:
        lines = [f"API options : OK ({len(known)} options lues, mkv→mp4)"]
    for key in ("hevc", "av1"):
        effective, opts = await fc_video_options(key, 23, "medium", input_format="mkv", api_key=api_key)
        if effective != key:
            lines.append(f"{CODECS[key].tag} : non proposé → repli H.264")
        else:
            lines.append(f"{CODECS[key].tag} : " + ", ".join(f"{k}={v}" for k, v in opts.items()))
    return "\n".join(lines)
