"""Paid time-limited access: grant/extend/revoke/expiry and the access
decision matrix."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from bot.database.connection import Database
from bot.services import access


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "access.db")
    await database.connect()
    yield database
    await database.close()


# ── grant / extend / revoke ──────────────────────────────────────────────

async def test_grant_creates_active_subscription(db):
    sub = await access.grant(db, 111, "alice", 30, granted_by=1, note="paid $5")
    assert sub.active
    assert 29 < sub.seconds_left / 86400 <= 30
    assert sub.note == "paid $5"
    assert await access.is_active(db, 111)


async def test_grant_extends_active_subscription(db):
    first = await access.grant(db, 111, "alice", 10, granted_by=1)
    second = await access.grant(db, 111, "alice", 5, granted_by=1)
    # 10 + 5 days, not reset to 5
    assert 14 < second.seconds_left / 86400 <= 15
    assert second.expires_at > first.expires_at


async def test_grant_after_expiry_starts_fresh(db):
    await access.grant(db, 111, "alice", 10, granted_by=1)
    # force-expire
    await db.execute("UPDATE subscriptions SET expires_at = ? WHERE user_id = ?",
                     (time.time() - 100, 111))
    assert not await access.is_active(db, 111)
    sub = await access.grant(db, 111, "alice", 7, granted_by=1)
    # fresh 7 days from now, not 7 - already-expired
    assert 6 < sub.seconds_left / 86400 <= 7
    assert sub.active


async def test_revoke(db):
    await access.grant(db, 111, "alice", 10, granted_by=1)
    assert await access.revoke(db, 111)
    assert not await access.is_active(db, 111)
    assert await access.get_subscription(db, 111) is None
    assert not await access.revoke(db, 111)  # already gone


async def test_list_active_excludes_expired(db):
    await access.grant(db, 111, "alice", 10, granted_by=1)
    await access.grant(db, 222, "bob", 10, granted_by=1)
    await db.execute("UPDATE subscriptions SET expires_at = ? WHERE user_id = ?",
                     (time.time() - 1, 222))
    active = await access.list_active(db)
    assert [s.user_id for s in active] == [111]
    assert len(await access.list_all(db)) == 2


# ── access decision matrix ───────────────────────────────────────────────

@dataclass
class _Cfg:
    owner_id: int = 1
    owner_only: bool = False
    subscription_mode: bool = False
    authorized_users: tuple = ()


class _RT:
    def __init__(self, db, **cfg):
        self.db = db
        self.config = _Cfg(**cfg)


async def test_owner_always_allowed(db):
    rt = _RT(db, subscription_mode=True)
    assert await access.access_denial_reason(rt, 1) is None


async def test_public_by_default(db):
    rt = _RT(db)  # no sub mode, no allowlist
    assert await access.access_denial_reason(rt, 999) is None


async def test_subscription_mode_denies_stranger(db):
    rt = _RT(db, subscription_mode=True)
    reason = await access.access_denial_reason(rt, 999)
    assert reason is not None and "subscription" in reason.lower()


async def test_subscription_mode_allows_active_sub(db):
    rt = _RT(db, subscription_mode=True)
    await access.grant(db, 999, "carol", 5, granted_by=1)
    assert await access.access_denial_reason(rt, 999) is None


async def test_expired_sub_gets_expiry_message(db):
    rt = _RT(db, subscription_mode=True)
    await access.grant(db, 999, "carol", 5, granted_by=1)
    await db.execute("UPDATE subscriptions SET expires_at = ? WHERE user_id = ?",
                     (time.time() - 1, 999))
    reason = await access.access_denial_reason(rt, 999)
    assert reason is not None and "expired" in reason.lower()


async def test_allowlist_grants_permanent_access(db):
    rt = _RT(db, subscription_mode=True, authorized_users=(555,))
    assert await access.access_denial_reason(rt, 555) is None


async def test_owner_only_blocks_even_subscribers(db):
    rt = _RT(db, owner_only=True)
    await access.grant(db, 999, "carol", 5, granted_by=1)
    assert await access.access_denial_reason(rt, 999) is not None
