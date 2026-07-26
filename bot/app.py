"""Application entry point: config → logging → DB → client → job manager.

Graceful shutdown: pyrogram's idle() returns on SIGINT/SIGTERM; the finally
block stops the job manager (terminating FFmpeg processes), closes the DB and
stops the client. Interrupted jobs are reconciled by recovery on next start.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from bot.config import Config, ConfigError
from bot.logging_config import setup_logging

log = logging.getLogger(__name__)


def _install_uvloop() -> None:
    try:
        import uvloop
        uvloop.install()
        log.info("uvloop active")
    except ImportError:
        pass


def build_client(cfg: Config):
    from pyrogram import Client

    kwargs: dict = dict(
        name="encoding_bot",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        bot_token=cfg.bot_token,
        plugins={"root": "bot.handlers"},
        workdir=str(cfg.session_dir),
    )
    # PyroFork pipelines several MTProto chunks at once — a large upload
    # speedup. Guarded by a signature check so other forks still start.
    import inspect
    if "max_concurrent_transmissions" in inspect.signature(Client.__init__).parameters:
        kwargs["max_concurrent_transmissions"] = 6
    return Client(**kwargs)


async def run() -> None:
    try:
        cfg = Config.load()
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg.secrets)
    log.info("Starting Zilong Encoding Bot")

    from pyrogram import idle
    from pyrogram.errors import FloodWait

    from bot.database.connection import Database
    from bot.jobs.manager import JobManager
    from bot.jobs.repository import JobRepository
    from bot.runtime import Runtime, set_runtime
    from bot.services.ffmpeg import check_ffmpeg_capabilities
    from bot.services.presets import load_presets, seed_presets

    db = Database(cfg.database_path)
    await db.connect()
    repo = JobRepository(db)
    await seed_presets(db)
    manager = JobManager(cfg, repo)

    capabilities = await check_ffmpeg_capabilities(cfg.ffmpeg_path)
    log.info("FFmpeg capabilities: %s",
             {k: v for k, v in capabilities.items()})
    if not capabilities.get("libass"):
        log.warning("FFmpeg has no 'subtitles' filter (libass) — "
                    "hardsubbing will fail. Install a full FFmpeg build.")

    client = build_client(cfg)
    rt = Runtime(config=cfg, db=db, repo=repo, manager=manager,
                 client=client, capabilities=capabilities,
                 presets=await load_presets(db))
    set_runtime(rt)

    for attempt in range(1, 6):
        try:
            await client.start()
            break
        except FloodWait as fw:
            wait = float(getattr(fw, "value", 30) or 30)
            log.warning("FloodWait %.0fs on login (attempt %d/5)", wait, attempt)
            if attempt == 5:
                raise
            await asyncio.sleep(wait + 5)

    me = await client.get_me()
    log.info("Logged in as @%s (id=%d)", me.username or me.first_name, me.id)

    if cfg.seedr_enabled:
        from bot.services.seedr import SeedrClient
        ok_count = 0
        for user, pwd in cfg.seedr_accounts:
            try:
                async with SeedrClient(user, pwd, proxy=cfg.seedr_proxy) as sc:
                    if await sc.check_credentials():
                        ok_count += 1
                    else:
                        log.warning("Seedr account %s: INVALID", user[:20])
            except Exception as exc:  # noqa: BLE001
                log.warning("Seedr account %s check failed: %s", user[:20], exc)
        log.info("Seedr accounts OK: %d/%d", ok_count, len(cfg.seedr_accounts))

    await manager.start()
    log.info("Job manager started (encodes=%d, downloads=%d)",
             cfg.max_concurrent_encodes, cfg.max_concurrent_downloads)

    try:
        await idle()
    finally:
        log.info("Shutting down…")
        await manager.stop()
        try:
            await client.stop()
        except Exception:  # noqa: BLE001
            pass
        await db.close()
        log.info("Shutdown complete.")


def main() -> None:
    _install_uvloop()
    asyncio.run(run())


if __name__ == "__main__":
    main()
