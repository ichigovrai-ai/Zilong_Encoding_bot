"""Dump channels: every uploaded file is copied/forwarded to the channels."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.config import Config, ConfigError
from bot.jobs import fc_pipeline, worker
from bot.jobs.states import JobStatus
from bot.services import dump

OWNER = 1


def _cfg(**kw) -> Config:
    base = dict(api_id=1, api_hash="h", bot_token="t", owner_id=OWNER)
    base.update(kw)
    return Config(**base)


def _sent(msg_id: int = 55, chat_id: int = 7):
    return SimpleNamespace(id=msg_id, chat=SimpleNamespace(id=chat_id))


class _Client:
    def __init__(self, fail: dict[int, Exception] | None = None):
        self.calls: list[tuple] = []
        self.fail = fail or {}

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.calls.append(("copy", chat_id, from_chat_id, message_id))
        if chat_id in self.fail:
            raise self.fail[chat_id]

    async def forward_messages(self, chat_id, from_chat_id, message_id):
        self.calls.append(("forward", chat_id, from_chat_id, message_id))
        if chat_id in self.fail:
            raise self.fail[chat_id]


# ── config ────────────────────────────────────────────────────────────────
def _load(tmp_path: Path, monkeypatch, **env) -> Config:
    for k, v in {"API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:t",
                 "OWNER_ID": "1", **env}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    return Config.load(env_file=str(tmp_path / ".noenv"))


def test_config_parses_dump_settings(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch,
                DUMP_CHANNELS="-1001234567890, -1009876543210",
                DUMP_MODE="Forward", DUMP_ALL_USERS="1")
    assert cfg.dump_channels == (-1001234567890, -1009876543210)
    assert cfg.dump_mode == "forward" and cfg.dump_all_users is True


def test_config_dump_defaults_to_off(tmp_path, monkeypatch):
    monkeypatch.delenv("DUMP_CHANNELS", raising=False)
    cfg = _load(tmp_path, monkeypatch)
    assert cfg.dump_channels == () and cfg.dump_mode == "copy"
    assert cfg.dump_all_users is False


@pytest.mark.parametrize("env", [
    {"DUMP_CHANNELS": "12345"},              # a user id, not a channel
    {"DUMP_CHANNELS": "-100123", "DUMP_MODE": "move"},
])
def test_config_rejects_bad_dump_settings(tmp_path, monkeypatch, env):
    with pytest.raises(ConfigError):
        _load(tmp_path, monkeypatch, **env)


# ── who gets dumped ───────────────────────────────────────────────────────
def test_only_owner_uploads_are_dumped_by_default():
    cfg = _cfg(dump_channels=(-100111,))
    assert dump.dump_targets(cfg, OWNER) == (-100111,)
    assert dump.dump_targets(cfg, 999) == ()                 # someone else
    everyone = _cfg(dump_channels=(-100111,), dump_all_users=True)
    assert dump.dump_targets(everyone, 999) == (-100111,)
    assert dump.dump_targets(_cfg(), OWNER) == ()            # nothing configured


# ── sending ───────────────────────────────────────────────────────────────
async def test_copy_to_every_channel():
    c = _Client()
    cfg = _cfg(dump_channels=(-100111, -100222))
    assert await dump.send_to_dumps(c, cfg, OWNER, _sent(55, 7)) == []
    assert c.calls == [("copy", -100111, 7, 55), ("copy", -100222, 7, 55)]


async def test_forward_mode_uses_forward():
    c = _Client()
    cfg = _cfg(dump_channels=(-100111,), dump_mode="forward")
    await dump.send_to_dumps(c, cfg, OWNER, _sent())
    assert c.calls[0][0] == "forward"


async def test_nothing_sent_for_other_users_or_empty_upload():
    c = _Client()
    cfg = _cfg(dump_channels=(-100111,))
    assert await dump.send_to_dumps(c, cfg, 999, _sent()) == []
    assert await dump.send_to_dumps(c, cfg, OWNER, None) == []
    assert await dump.send_to_dumps(c, cfg, OWNER,
                                    SimpleNamespace(id=None)) == []
    assert c.calls == []


async def test_one_broken_channel_does_not_block_the_others():
    class ChatAdminRequired(Exception):
        pass

    c = _Client(fail={-100111: ChatAdminRequired("no rights")})
    cfg = _cfg(dump_channels=(-100111, -100222))
    problems = await dump.send_to_dumps(c, cfg, OWNER, _sent())
    assert [call[1] for call in c.calls] == [-100111, -100222]   # both tried
    assert len(problems) == 1
    assert "-100111" in problems[0] and "admin" in problems[0]


async def test_flood_wait_is_retried_once(monkeypatch):
    from pyrogram.errors import FloodWait

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(dump.asyncio, "sleep", _no_sleep)

    class _Flaky(_Client):
        async def copy_message(self, chat_id, from_chat_id, message_id):
            self.calls.append(("copy", chat_id))
            if len(self.calls) == 1:
                raise FloodWait(value=3)

    c = _Flaky()
    cfg = _cfg(dump_channels=(-100111,))
    assert await dump.send_to_dumps(c, cfg, OWNER, _sent()) == []
    assert len(c.calls) == 2


async def test_startup_check_logs_and_never_raises(caplog):
    class _C:
        async def get_chat(self, chat_id):
            if chat_id == -100222:
                raise RuntimeError("PEER_ID_INVALID")
            return SimpleNamespace(title="My dump")

    import logging
    with caplog.at_level(logging.INFO, logger="bot.services.dump"):
        await dump.check_dump_channels(
            _C(), _cfg(dump_channels=(-100111, -100222)))
    text = caplog.text
    assert "Dump channel OK: My dump" in text
    assert "-100222 NOT reachable" in text


# ── wiring: the pipelines hand the uploaded message to the dump ───────────
from tests.test_fc_pipeline import _queued_job, _run, env  # noqa: E402,F401


async def test_fc_pipeline_dumps_each_uploaded_file(env):
    sent_msgs, dumped = [], []

    async def fake_upload(client, chat_id, path, **kw):
        m = _sent(100 + len(sent_msgs), chat_id)
        sent_msgs.append(m)
        return m

    async def fake_dump(client, cfg, user_id, sent, **kw):
        dumped.append((user_id, sent))
        return []

    env.monkeypatch.setattr(fc_pipeline, "upload_file", fake_upload)
    env.monkeypatch.setattr(fc_pipeline, "send_to_dumps", fake_dump)

    async def fake_multi(api_keys, video_url, source_name, subtitle_path,
                         dest_dir, *, qualities, **kw):
        out = {}
        for label in qualities:
            f = Path(dest_dir) / f"Show {label}.mp4"
            f.write_bytes(b"\0" * 2048)
            out[label] = str(f)
        return out

    env.monkeypatch.setattr(fc_pipeline.freeconvert,
                            "hardsub_remote_url_multi", fake_multi)
    job = await _queued_job(env, source="upload", fc_res=720,
                            fc_res_list=[720, 480])
    await _run(env, job)

    assert len(dumped) == 2 and [d[1] for d in dumped] == sent_msgs
    assert {d[0] for d in dumped} == {job.user_id}
    assert (await env.rt.repo.get(job.id)).status == JobStatus.COMPLETED


async def test_a_failing_dump_never_fails_the_job(env):
    async def fake_upload(client, chat_id, path, **kw):
        return _sent()

    async def boom(client, cfg, user_id, sent, **kw):
        return ["-100111: make the bot an admin of that channel"]

    env.monkeypatch.setattr(fc_pipeline, "upload_file", fake_upload)
    env.monkeypatch.setattr(fc_pipeline, "send_to_dumps", boom)
    job = await _queued_job(env, source="upload", fc_res=480)
    await _run(env, job)
    assert (await env.rt.repo.get(job.id)).status == JobStatus.COMPLETED
