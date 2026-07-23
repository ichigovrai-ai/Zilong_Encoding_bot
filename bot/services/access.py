"""Paid, time-limited access grants (subscriptions).

The owner grants a user N days of access with /grant; access is checked on
every job submission and expires automatically. Grants STACK: giving days to
a user who is still active adds to their remaining time (paying again extends
the window rather than resetting it).

Access decision precedence (`access_denial_reason`):
  1. owner                → always allowed
  2. OWNER_ONLY mode      → only the owner, everyone else denied
  3. AUTHORIZED_USERS     → permanent free allowlist, always allowed
  4. active subscription  → allowed until expiry
  5. SUBSCRIPTION_MODE on, or an allowlist exists → others denied
  6. otherwise            → public (unchanged default)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.database.connection import Database
from bot.utils.timefmt import fmt_date, fmt_days

if TYPE_CHECKING:
    from bot.runtime import Runtime

_DAY = 86400.0


@dataclass(slots=True)
class Subscription:
    user_id: int
    username: str
    granted_by: int
    granted_at: float
    expires_at: float
    note: str

    @property
    def active(self) -> bool:
        return self.expires_at > time.time()

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.expires_at - time.time())


def _row(r) -> Subscription:
    return Subscription(
        user_id=r["user_id"], username=r["username"], granted_by=r["granted_by"],
        granted_at=r["granted_at"], expires_at=r["expires_at"], note=r["note"],
    )


async def get_subscription(db: Database, user_id: int) -> Subscription | None:
    r = await db.fetchone("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
    return _row(r) if r else None


async def is_active(db: Database, user_id: int) -> bool:
    r = await db.fetchone(
        "SELECT 1 FROM subscriptions WHERE user_id = ? AND expires_at > ?",
        (user_id, time.time()),
    )
    return r is not None


async def grant(db: Database, user_id: int, username: str, days: int,
                granted_by: int, note: str = "") -> Subscription:
    """Grant or extend access by `days`. Extends from the later of now or the
    current expiry, so paying while still active adds time."""
    now = time.time()
    existing = await get_subscription(db, user_id)
    base = max(now, existing.expires_at) if existing else now
    expires_at = base + days * _DAY
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO subscriptions "
            "(user_id, username, granted_by, granted_at, expires_at, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username = excluded.username, granted_by = excluded.granted_by, "
            "granted_at = excluded.granted_at, expires_at = excluded.expires_at, "
            "note = excluded.note",
            (user_id, username, granted_by, now, expires_at, note, now),
        )
    sub = await get_subscription(db, user_id)
    assert sub is not None
    return sub


async def revoke(db: Database, user_id: int) -> bool:
    if await get_subscription(db, user_id) is None:
        return False
    await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    return True


async def list_active(db: Database) -> list[Subscription]:
    rows = await db.fetchall(
        "SELECT * FROM subscriptions WHERE expires_at > ? ORDER BY expires_at",
        (time.time(),),
    )
    return [_row(r) for r in rows]


async def list_all(db: Database) -> list[Subscription]:
    rows = await db.fetchall(
        "SELECT * FROM subscriptions ORDER BY expires_at DESC"
    )
    return [_row(r) for r in rows]


async def access_denial_reason(rt: "Runtime", user_id: int) -> str | None:
    """None if the user may use the bot, else a user-facing refusal message."""
    cfg = rt.config
    if user_id == cfg.owner_id:
        return None
    if cfg.owner_only:
        return "⛔ This bot is private (owner only)."
    if user_id in cfg.authorized_users:
        return None
    sub = await get_subscription(rt.db, user_id)
    if sub and sub.active:
        return None
    if sub and not sub.active:
        return (
            f"⌛ <b>Your access expired</b> on {fmt_date(sub.expires_at)}.\n"
            "Contact the owner to renew your subscription."
        )
    if cfg.subscription_mode or cfg.authorized_users:
        return (
            "🔒 <b>You don't have access to this bot.</b>\n"
            "It's subscription-based — contact the owner to get access."
        )
    return None


def status_text(rt: "Runtime", user_id: int,
                sub: Subscription | None) -> str:
    """One-line access status for the user, used by /access."""
    cfg = rt.config
    if user_id == cfg.owner_id:
        return "👑 You are the owner — unlimited access."
    if user_id in cfg.authorized_users:
        return "✅ You have permanent access (allowlisted)."
    if sub and sub.active:
        return (f"✅ Active until <b>{fmt_date(sub.expires_at)}</b> "
                f"({fmt_days(sub.seconds_left)} left).")
    if sub and not sub.active:
        return (f"⌛ Expired on <b>{fmt_date(sub.expires_at)}</b>. "
                "Contact the owner to renew.")
    if cfg.subscription_mode or cfg.authorized_users:
        return "🔒 No active subscription. Contact the owner to get access."
    return "✅ This bot is currently open to everyone."
