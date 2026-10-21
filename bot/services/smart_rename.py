"""
bot/services/smart_rename.py

Rename automatique du fichier final avant upload — UN SEUL format :

    <Titre> EP<xx> VOSTFR.<extension>

    Dragon_Ball_Super_Beerus_S01E01_1080p_CR_WEB-DL_AAC2.0_H.264-VARYG.mkv
        -> Dragon Ball Super Beerus EP01 VOSTFR.mkv
    [Erai-raws] Rilakkuma - 23 [1080p CR WEB-DL AVC AAC][MultiSub][45752258].mkv
        -> Rilakkuma EP23 VOSTFR.mkv
    _Meda-Arcedo_ Virgin Punk Clockwork Girl VOSTFR.mp4        (pas d'épisode)
        -> Virgin Punk Clockwork Girl VOSTFR.mp4

Règles :
  - Les séparateurs  _  .  -  :  ,  +  sont remplacés par un espace (puis les
    espaces multiples sont fusionnés) : "Dragon_Ball-Super" -> "Dragon Ball Super".
  - Tout ce qui est entre [crochets] ou (parenthèses) disparaît (groupe,
    hash, source...), ainsi que le tag de groupe final (-VARYG, -Tsundere-Raws).
  - Le titre s'arrête au premier tag technique (langue, 1080p, WEB-DL, H.264,
    AAC, plateforme...) ou au marqueur d'épisode.
  - Épisode reconnu sous la forme S01E05, E05 / EP05 / Episode 5, ou "Titre - 05".
    La SAISON est abandonnée (S02E05 -> EP05).
  - Les caractères interdits dans un nom de fichier  / \\ * ? " < > |  sont retirés.
  - Qualité et codec n'apparaissent PLUS dans le nom (voir keep_quality_tag
    pour le seul cas où plusieurs qualités sortent d'un même job).
  - Si aucun titre n'est trouvé, le nom d'origine est renvoyé tel quel
    (mieux vaut un nom non modifié qu'un nom cassé).
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

# Étiquette de langue écrite dans tous les noms (les sorties sont hardsub FR).
LANG_TAG = "VOSTFR"

_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")

# ── Tags techniques : le titre s'arrête au premier d'entre eux ────────────
_LANG_ALT = r"VOSTFR|VFF?Q?|MULTI(?:-?SUBS?)?|DUAL|SUBFRENCH|FRENCH|TRUEFRENCH"
_RES_ALT = r"\d{3,4}p|\d{3,4}x\d{3,4}|4K"
_SOURCE_ALT = r"WEB-?DL|WEBRip|BDRip|Blu-?Ray|HDTV"
_VIDEO_ALT = r"H\.?26[45]|x26[45]|HEVC|AVC|AV1"
_AUDIO_ALT = r"AAC(?:\d(?:[.\s]?\d)?)?|AC-?3|DDP?\d(?:\.\d)?|FLAC|OPUS|DTS|MP3"
_PLATFORM_ALT = (
    r"CR|ADN|DSNP|NF|AMZN|HULU|HIDIVE|FUNI|ABEMA|VRV|WAKANIM|B-?Global|iQ(?:iyi)?|U-?NEXT"
)

_TECH_RE = re.compile(
    r"\b(?:" + "|".join((_LANG_ALT, _RES_ALT, _SOURCE_ALT, _VIDEO_ALT, _AUDIO_ALT, _PLATFORM_ALT)) + r")\b",
    re.IGNORECASE,
)

# ── Marqueur d'épisode (par ordre de priorité) ────────────────────────────
_EP_PATTERNS = (
    re.compile(r"\bS\d{1,2}[ .]?E(?P<n>\d{1,4})\b", re.IGNORECASE),        # S01E05
    re.compile(r"\b(?:EP|Episode)[ .]?(?P<n>\d{1,4})\b", re.IGNORECASE),   # EP05 / Episode 5
    re.compile(r"\bE(?P<n>\d{1,4})\b", re.IGNORECASE),                     # E05
    re.compile(r"\s-\s(?P<n>\d{1,4})(?:v\d)?\b"),                          # Titre - 05
)

_LEADING_TAG_RE = re.compile(r"^_[^_]+_\s*")          # _Meda-Arcedo_ Titre
_BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_SEPARATORS_RE = re.compile(r"[.\-:,+\u2013\u2014]")  # . - : , + – —
_FORBIDDEN_RE = re.compile(r'[\\/*?"<>|]')


def resolution_label(height: int) -> str:
    return f"{height}p"


def _tidy(text: str) -> str:
    """Séparateurs -> espace, caractères interdits retirés, espaces fusionnés."""
    text = text.replace("_", " ")
    text = _SEPARATORS_RE.sub(" ", text)
    text = _FORBIDDEN_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _cut_at_tech_token(text: str) -> str:
    m = _TECH_RE.search(text)
    return text[:m.start()] if m else text


def _split_title_episode(base: str) -> tuple[str, int | None]:
    """(titre propre, numéro d'épisode ou None) à partir du nom sans extension."""
    text = _LEADING_TAG_RE.sub("", base.strip())
    text = _BRACKETS_RE.sub(" ", text)
    text = text.replace("_", " ")   # le "_" est un caractère de mot : il casserait les \b

    for pattern in _EP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        episode = int(match.group("n"))
        title = _tidy(_cut_at_tech_token(text[:match.start()]))
        if not title:  # "S01E01 Titre..." : le titre est après le marqueur
            title = _tidy(_cut_at_tech_token(text[match.end():]))
        return title, episode

    return _tidy(_cut_at_tech_token(text)), None


def build_final_name(
    real_filename: str,
    *,
    override_quality: str | None = None,
    override_codec: str | None = None,   # accepté pour compatibilité, ignoré (plus de codec dans le nom)
    output_ext: str | None = None,
    keep_quality_tag: bool = False,
) -> str:
    """Nom final : "<Titre> EP<xx> VOSTFR<ext>".

    override_quality n'est écrit dans le nom que si keep_quality_tag=True
    (utilisé quand un même job sort PLUSIEURS qualités : sans ça, "480p" et
    "360p" auraient le même nom et s'écraseraient).
    """
    base, ext = os.path.splitext(real_filename)
    if not _EXT_RE.match(ext):          # "Dr. Stone" n'a pas d'extension " Stone"
        base, ext = real_filename, ""

    title, episode = _split_title_episode(base)
    if not title:
        return real_filename

    parts = [title]
    if episode is not None:
        parts.append(f"EP{episode:02d}")
    parts.append(LANG_TAG)
    if keep_quality_tag and override_quality:
        parts.append(override_quality)

    final_ext = f".{output_ext.lstrip('.')}" if output_ext else ext
    return f"{' '.join(parts)}{final_ext}"


def finalize_download(
    real_path: str,
    *,
    override_quality: str | None = None,
    override_codec: str | None = None,
    output_ext: str | None = None,
    keep_quality_tag: bool = False,
) -> str:
    """
    Renomme SUR LE DISQUE un fichier déjà téléchargé sous son vrai nom pour
    qu'il porte le nom final reconstruit (voir build_final_name).

    Contrairement au pipeline FreeConvert (hardsub_remote_url dans
    freeconvert.py), qui calcule le nom final AVANT de télécharger et
    écrit donc directement dedans (jamais de "vrai nom" sur le disque à
    supprimer), cette fonction sert aux pipelines qui téléchargent d'abord
    sous le nom réel de la source, puis doivent le remplacer après coup.

    Renvoie le nouveau chemin complet (identique à real_path si le nom
    n'a pas changé). os.replace() est atomique et écrase une éventuelle
    destination déjà existante.
    """
    real_dir = os.path.dirname(real_path)
    real_name = os.path.basename(real_path)

    new_name = build_final_name(
        real_name, override_quality=override_quality, override_codec=override_codec,
        output_ext=output_ext, keep_quality_tag=keep_quality_tag,
    )
    if new_name == real_name:
        return real_path

    new_path = os.path.join(real_dir, new_name)

    if not os.path.exists(real_path):
        log.warning("finalize_download: fichier source introuvable: %s", real_path)
        return real_path

    try:
        os.replace(real_path, new_path)
    except OSError as exc:
        log.warning(
            "finalize_download: échec du renommage %s -> %s (%s), fichier conservé sous son vrai nom.",
            real_path, new_path, exc,
        )
        return real_path

    return new_path


if __name__ == "__main__":
    tests = [
        "Dragon_Ball_Super_Beerus_S01E01_1080p_CR_WEB-DL_AAC2.0_H.264-VARYG.mkv",
        "RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws.mkv",
        "[Erai-raws] Rilakkuma - 23 [1080p CR WEB-DL AVC AAC][MultiSub][45752258].mkv",
        "[SubsPlease] Rilakkuma - 23 (480p) [E2E141F1].mkv",
        "Though.I.Am.an.Inept.Villainess.S01E09.I.Will.Make.Sure.to.Save.You.1080p.DSNP.WEB-DL.AAC2.0.H.264-VARYG.mkv",
        "[Team Arcedo] Onee-chan Gokko S01E01 VOSTFR (WEB-DL 1080p H.264 AAC).mkv",
        "Mushoku_Tensei_Jobless_Reincarnation_S03E08_The_Flying_Fortress.mp4",
        "_Meda-Arcedo_ Virgin Punk Clockwork Girl VOSTFR.mp4",
        "Spy x Family: Code, White + Extra EP5.mkv",
        "UNPARSEABLE.mkv",
    ]
    for name in tests:
        print(f"{name}\n  -> {build_final_name(name)}\n")
