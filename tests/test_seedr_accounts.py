"""Multi-account Seedr: config parsing, credential lookup, best-account pick."""
from __future__ import annotations

import os

import pytest

from bot.config import Config, ConfigError


BASE_ENV = {
    "API_ID": "123", "API_HASH": "hash", "BOT_TOKEN": "tok", "OWNER_ID": "1",
}


def _load(monkeypatch, tmp_path, **extra):
    for k in ("SEEDR_USERNAME", "SEEDR_PASSWORD", "SEEDR_PROXY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**BASE_ENV, **extra}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "w"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "d/bot.db"))
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dl"))
    return Config.load(env_file=str(tmp_path / ".noenv"))


def test_single_account(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path,
                SEEDR_USERNAME="a@x.com", SEEDR_PASSWORD="p1")
    assert cfg.seedr_accounts == (("a@x.com", "p1"),)
    assert cfg.seedr_enabled
    assert cfg.seedr_username == "a@x.com" and cfg.seedr_password == "p1"


def test_multi_account(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path,
                SEEDR_USERNAME="a@x.com, b@x.com",
                SEEDR_PASSWORD="p1, p2")
    assert cfg.seedr_accounts == (("a@x.com", "p1"), ("b@x.com", "p2"))
    assert cfg.seedr_credentials("b@x.com") == ("b@x.com", "p2")
    # unknown username → first account
    assert cfg.seedr_credentials("nope") == ("a@x.com", "p1")
    # passwords are redaction secrets
    assert "p1" in cfg.secrets and "p2" in cfg.secrets


def test_no_account(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path)
    assert cfg.seedr_accounts == ()
    assert not cfg.seedr_enabled


def test_count_mismatch_errors(monkeypatch, tmp_path):
    with pytest.raises(ConfigError) as ei:
        _load(monkeypatch, tmp_path,
              SEEDR_USERNAME="a@x.com, b@x.com", SEEDR_PASSWORD="p1")
    assert "counts must match" in str(ei.value)


async def test_pick_best_account(monkeypatch):
    from bot.services import seedr as seedr_mod

    storages = {"a": (2000, 1800), "b": (2000, 100)}  # b has more free

    class _FakeSC:
        def __init__(self, user, pwd, proxy=""):
            self.user = user

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def storage(self):
            return storages[self.user]

    monkeypatch.setattr(seedr_mod, "SeedrClient", _FakeSC)
    user, pwd, free = await seedr_mod.pick_best_account([("a", "pa"), ("b", "pb")])
    assert user == "b"          # emptiest account chosen
