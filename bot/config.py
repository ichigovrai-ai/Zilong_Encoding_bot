"""Application configuration.

All secrets and tunables come from environment variables (a local .env file is
loaded first when present). `Config.load()` validates everything at once and
raises `ConfigError` listing every problem, so a misconfigured deployment
fails fast with a complete report instead of dying on the first missing var.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _split_ids(raw: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in raw.replace(",", " ").split():
        p = part.strip()
        if p.lstrip("-").isdigit():
            out.append(int(p))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Config:
    # Telegram
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    authorized_users: tuple[int, ...] = ()
    owner_only: bool = False
    # When on, only the owner, the AUTHORIZED_USERS allowlist, and users with
    # an active paid grant may use the bot. Off = current public behaviour.
    subscription_mode: bool = False

    # Paths
    work_dir: Path = Path("work")
    download_dir: Path = Path("downloads")
    database_path: Path = Path("data/bot.db")
    session_dir: Path = Path("data")

    # External binaries
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    mediainfo_path: str = "mediainfo"
    aria2c_path: str = ""

    # Concurrency / limits
    max_concurrent_encodes: int = 1
    max_concurrent_downloads: int = 2
    max_concurrent_uploads: int = 2
    max_jobs_per_user: int = 5
    max_input_size: int = 4 * 1024**3
    max_output_size: int = 2 * 1024**3
    min_free_disk_gb: float = 2.0
    job_timeout_hours: float = 6.0
    job_retention_hours: float = 72.0

    # Encoding defaults
    default_video_codec: str = "libx264"
    default_audio_codec: str = "aac"
    default_crf: int = 23
    default_preset: str = "medium"
    default_resolution: str = "original"
    fallback_font: str = "Arial"
    # Preferred embedded-subtitle language to burn when a Seedr video has no
    # matched external subtitle (e.g. "fr" for VOSTFR releases). Empty = pick
    # the default-disposition track, else the first text subtitle.
    preferred_sub_lang: str = ""

    # Seedr
    seedr_username: str = ""
    seedr_password: str = ""
    # HTTP proxy for Seedr API calls. Needed on datacenter IPs (EC2, Colab,
    # Render …) because Seedr blocks add_torrent from those ranges while
    # read-only calls still work. CDN downloads never use the proxy.
    seedr_proxy: str = ""
    seedr_auto_delete: bool = True
    seedr_poll_interval: float = 10.0
    seedr_timeout_hours: float = 4.0

    # UX
    progress_update_interval: float = 4.0

    # Derived at load time
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @property
    def seedr_enabled(self) -> bool:
        return bool(self.seedr_username and self.seedr_password)

    def is_authorized(self, user_id: int) -> bool:
        if user_id == self.owner_id:
            return True
        if self.owner_only:
            return False
        if self.authorized_users:
            return user_id in self.authorized_users
        return True

    @classmethod
    def load(cls, env_file: str | None = None) -> "Config":
        load_dotenv(env_file or ".env", override=True)
        errors: list[str] = []

        def req(name: str) -> str:
            val = os.environ.get(name, "").strip()
            if not val:
                errors.append(f"{name} is required but not set")
            return val

        def opt(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip() or default

        def num(name: str, default: float, lo: float, hi: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                v = float(raw)
            except ValueError:
                errors.append(f"{name} must be a number, got {raw!r}")
                return default
            if not lo <= v <= hi:
                errors.append(f"{name}={v} outside allowed range [{lo}, {hi}]")
                return default
            return v

        api_id_raw = req("API_ID")
        api_hash = req("API_HASH")
        bot_token = req("BOT_TOKEN")
        owner_raw = req("OWNER_ID")

        api_id = 0
        if api_id_raw and not api_id_raw.isdigit():
            errors.append("API_ID must be numeric")
        elif api_id_raw:
            api_id = int(api_id_raw)
        owner_id = 0
        if owner_raw and not owner_raw.lstrip("-").isdigit():
            errors.append("OWNER_ID must be numeric")
        elif owner_raw:
            owner_id = int(owner_raw)

        seedr_user = opt("SEEDR_USERNAME")
        seedr_pwd = opt("SEEDR_PASSWORD")
        if bool(seedr_user) != bool(seedr_pwd):
            errors.append("SEEDR_USERNAME and SEEDR_PASSWORD must be set together")
        seedr_proxy = opt("SEEDR_PROXY")
        if seedr_proxy and not seedr_proxy.startswith(("http://", "https://")):
            errors.append("SEEDR_PROXY must be an http(s):// proxy URL "
                          "(SOCKS is not supported)")

        ffmpeg_path = opt("FFMPEG_PATH", "ffmpeg")
        ffprobe_path = opt("FFPROBE_PATH", "ffprobe")
        for label, binary in (("FFMPEG_PATH", ffmpeg_path), ("FFPROBE_PATH", ffprobe_path)):
            if shutil.which(binary) is None and not Path(binary).is_file():
                errors.append(f"{label}: executable not found: {binary!r}")

        if errors:
            raise ConfigError(
                "Configuration invalid:\n  - " + "\n  - ".join(errors)
            )

        secrets = tuple(s for s in (bot_token, api_hash, seedr_pwd,
                                    seedr_proxy) if s)

        cfg = cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            owner_id=owner_id,
            authorized_users=_split_ids(opt("AUTHORIZED_USERS")),
            owner_only=opt("OWNER_ONLY", "0").lower() in ("1", "true", "yes"),
            subscription_mode=opt("SUBSCRIPTION_MODE", "0").lower()
            in ("1", "true", "yes"),
            work_dir=Path(opt("WORK_DIR", "work")),
            download_dir=Path(opt("DOWNLOAD_DIR", "downloads")),
            database_path=Path(opt("DATABASE_PATH", "data/bot.db")),
            session_dir=Path(opt("SESSION_DIR", "data")),
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            mediainfo_path=opt("MEDIAINFO_PATH", "mediainfo"),
            aria2c_path=opt("ARIA2C_PATH"),
            max_concurrent_encodes=int(num("MAX_CONCURRENT_ENCODES", 1, 1, 8)),
            max_concurrent_downloads=int(num("MAX_CONCURRENT_DOWNLOADS", 2, 1, 8)),
            max_concurrent_uploads=int(num("MAX_CONCURRENT_UPLOADS", 2, 1, 8)),
            max_jobs_per_user=int(num("MAX_JOBS_PER_USER", 5, 1, 100)),
            max_input_size=int(num("MAX_INPUT_SIZE_MB", 4096, 16, 64 * 1024)) * 1024**2,
            max_output_size=int(num("MAX_OUTPUT_SIZE_MB", 2000, 16, 64 * 1024)) * 1024**2,
            min_free_disk_gb=num("MIN_FREE_DISK_GB", 2.0, 0.0, 1024.0),
            job_timeout_hours=num("JOB_TIMEOUT_HOURS", 6.0, 0.1, 48.0),
            job_retention_hours=num("JOB_RETENTION_HOURS", 72.0, 1.0, 24 * 30),
            default_video_codec=opt("DEFAULT_VIDEO_CODEC", "libx264"),
            default_audio_codec=opt("DEFAULT_AUDIO_CODEC", "aac"),
            default_crf=int(num("DEFAULT_CRF", 23, 0, 51)),
            default_preset=opt("DEFAULT_PRESET", "medium"),
            default_resolution=opt("DEFAULT_RESOLUTION", "original"),
            fallback_font=opt("FALLBACK_FONT", "Arial"),
            preferred_sub_lang=opt("PREFERRED_SUB_LANG", ""),
            seedr_username=seedr_user,
            seedr_password=seedr_pwd,
            seedr_proxy=seedr_proxy,
            seedr_auto_delete=opt("SEEDR_AUTO_DELETE", "1").lower() in ("1", "true", "yes"),
            seedr_poll_interval=num("SEEDR_POLL_INTERVAL", 10.0, 3.0, 120.0),
            seedr_timeout_hours=num("SEEDR_TIMEOUT_HOURS", 4.0, 0.1, 24.0),
            progress_update_interval=num("PROGRESS_UPDATE_INTERVAL", 4.0, 2.0, 60.0),
            secrets=secrets,
        )

        for p in (cfg.work_dir, cfg.download_dir, cfg.session_dir, cfg.database_path.parent):
            p.mkdir(parents=True, exist_ok=True)
        return cfg
