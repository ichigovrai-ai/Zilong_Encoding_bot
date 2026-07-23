"""Async Seedr.cc client (aiohttp).

Protocol knowledge carried over from the audited project (`services/seedr.py`
v6 there): OAuth password grant against the Kodi/XBMC endpoint
(`client_id=seedr_xbmc`), then `resource.php` function calls. Works for free
and premium accounts (the REST v1 Basic-Auth API rejects free accounts).

Completion detection: after `add_torrent`, the transfer appears in the root
listing under `torrents[]` with a progress %; when Seedr finishes, the
transfer disappears and a FOLDER with (approximately) the torrent's name
materialises. Attribution is by normalised-name match — never "the only new
folder", because an account can run several transfers at once. Seedr's meter
also sometimes sticks at 98-99% after the folder is complete, so a folder
whose content signature is stable across three polls (or matches the torrent
size) is accepted early.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from bot.utils.subprocesses import CancelToken

log = logging.getLogger(__name__)

_OAUTH_URL = "https://www.seedr.cc/oauth_test/token"
_RESOURCE_URL = "https://www.seedr.cc/oauth_test/resource.php"
_CLIENT_ID = "seedr_xbmc"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class SeedrError(RuntimeError):
    pass


class SeedrAuthError(SeedrError):
    pass


class SeedrQuotaError(SeedrError):
    pass


class SeedrTimeout(SeedrError):
    pass


@dataclass(slots=True)
class SeedrFile:
    id: int
    name: str
    size: int
    path: str = ""      # folder-relative path built during recursive walks


@dataclass(slots=True)
class SeedrFolderInfo:
    id: int
    name: str
    size: int


@dataclass(slots=True)
class SeedrTorrent:
    id: int | None
    name: str
    progress: float
    size: int


@dataclass(slots=True)
class SeedrListing:
    folders: list[SeedrFolderInfo] = field(default_factory=list)
    files: list[SeedrFile] = field(default_factory=list)
    torrents: list[SeedrTorrent] = field(default_factory=list)
    space_used: int = 0
    space_max: int = 0


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


class SeedrClient:
    def __init__(self, username: str, password: str,
                 session: aiohttp.ClientSession | None = None,
                 proxy: str = ""):
        self._username = username
        self._password = password
        self._session = session
        self._own_session = session is None
        # HTTP proxy for API calls only (OAuth + resource.php). Seedr blocks
        # add_torrent from datacenter IP ranges (EC2, Colab, …) while
        # read-only calls work; CDN downloads happen elsewhere and never
        # go through the proxy.
        self._proxy = proxy or None
        self._token = ""
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> "SeedrClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": _UA},
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("SeedrClient used outside 'async with'")
        return self._session

    # ── Auth ────────────────────────────────────────────────────────────

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if self._token and not force:
                return self._token
            try:
                async with self.session.post(_OAUTH_URL, proxy=self._proxy, data={
                    "grant_type": "password",
                    "client_id": _CLIENT_ID,
                    "username": self._username,
                    "password": self._password,
                }) as resp:
                    body = await resp.json(content_type=None)
            except aiohttp.ClientError as exc:
                raise SeedrError(f"Seedr is unreachable: {exc}") from exc
            token = (body or {}).get("access_token", "")
            if not token:
                raise SeedrAuthError(
                    "Seedr login failed — check SEEDR_USERNAME / SEEDR_PASSWORD."
                )
            self._token = token
            return token

    async def _call(self, func: str, retry_auth: bool = True,
                    **params: Any) -> dict:
        token = await self._get_token()
        data = {"access_token": token, "func": func, **params}
        try:
            async with self.session.post(_RESOURCE_URL, proxy=self._proxy,
                                         data=data) as resp:
                if resp.status in (401, 403) and retry_auth:
                    await self._get_token(force=True)
                    return await self._call(func, retry_auth=False, **params)
                resp.raise_for_status()
                body = await resp.json(content_type=None)
        except aiohttp.ClientResponseError as exc:
            raise SeedrError(f"Seedr API error ({func}): HTTP {exc.status}") from exc
        except aiohttp.ClientError as exc:
            raise SeedrError(f"Seedr API unreachable ({func}): {exc}") from exc
        if not isinstance(body, dict):
            raise SeedrError(f"Unexpected Seedr response for {func}")
        return body

    async def check_credentials(self) -> bool:
        try:
            await self._get_token(force=True)
            return True
        except SeedrAuthError:
            return False

    # ── Listing ─────────────────────────────────────────────────────────

    async def list_folder(self, folder_id: int = 0) -> SeedrListing:
        body = self._normalise(await self._call("list_contents",
                                                folder_id=str(folder_id)))
        return body

    @staticmethod
    def _normalise(raw: dict) -> SeedrListing:
        # Some responses nest the payload under result/folder/data.
        actual = raw
        for key in ("result", "folder", "data"):
            candidate = raw.get(key)
            if isinstance(candidate, dict) and (
                "files" in candidate or "folders" in candidate or "torrents" in candidate
            ):
                actual = candidate
                break
        listing = SeedrListing(
            space_used=int(actual.get("space_used") or 0),
            space_max=int(actual.get("space_max") or 0),
        )
        for f in actual.get("folders") or []:
            try:
                listing.folders.append(SeedrFolderInfo(
                    id=int(f["id"]), name=str(f.get("name") or ""),
                    size=int(f.get("size") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        for f in actual.get("files") or actual.get("folder_files") or []:
            fid = None
            for key in ("folder_file_id", "id", "file_id"):
                if f.get(key) is not None:
                    try:
                        fid = int(f[key])
                        break
                    except (TypeError, ValueError):
                        continue
            if fid is None:
                continue
            listing.files.append(SeedrFile(
                id=fid, name=str(f.get("name") or "file"),
                size=int(f.get("size") or 0),
            ))
        for t in actual.get("torrents") or []:
            tid = t.get("id")
            try:
                tid = int(tid) if tid is not None else None
            except (TypeError, ValueError):
                tid = None
            raw_progress = str(t.get("progress") or "0").replace("%", "").strip()
            try:
                progress = float(raw_progress)
            except ValueError:
                progress = 0.0
            listing.torrents.append(SeedrTorrent(
                id=tid, name=str(t.get("name") or ""), progress=progress,
                size=int(t.get("size") or 0),
            ))
        return listing

    async def walk_folder(self, folder_id: int, *, max_depth: int = 6) -> list[SeedrFile]:
        """Recursively collect all files under a folder, with relative paths."""
        out: list[SeedrFile] = []

        async def _walk(fid: int, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            listing = await self.list_folder(fid)
            for f in listing.files:
                out.append(SeedrFile(f.id, f.name, f.size,
                                     path=f"{prefix}{f.name}"))
            for sub in listing.folders:
                await _walk(sub.id, f"{prefix}{sub.name}/", depth + 1)

        await _walk(folder_id, "", 0)
        return out

    # ── Actions ─────────────────────────────────────────────────────────

    async def add_magnet(self, magnet: str) -> int | None:
        body = await self._call("add_torrent", torrent_magnet=magnet)
        rv = body.get("result")
        if rv is False or str(rv).lower() == "false":
            err = str(body.get("error") or body.get("message") or "").lower()
            if "space" in err or "storage" in err or "quota" in err:
                raise SeedrQuotaError("Seedr storage is full — free up space first.")
            if "premium" in err or "wishlist" in err:
                raise SeedrQuotaError(
                    "Seedr put this torrent on the wishlist (account limit reached)."
                )
            raise SeedrError(f"Seedr rejected the magnet: "
                             f"{body.get('error') or body.get('message') or 'unknown error'}")
        for key in ("user_torrent_id", "torrent_id", "id"):
            if body.get(key):
                try:
                    return int(body[key])
                except (TypeError, ValueError):
                    continue
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("user_torrent_id", "torrent_id"):
                if data.get(key):
                    try:
                        return int(data[key])
                    except (TypeError, ValueError):
                        continue
        if isinstance(rv, int) and rv > 1:
            return rv
        return None  # accepted, but no id — poller falls back to name matching

    async def fetch_file_url(self, file_id: int) -> str:
        body = await self._call("fetch_file", folder_file_id=str(file_id))
        url = body.get("url") or body.get("download_url") or ""
        if not url:
            for key in ("result", "data"):
                inner = body.get(key)
                if isinstance(inner, dict):
                    url = inner.get("url") or inner.get("download_url") or ""
                elif isinstance(inner, str) and inner.startswith("http"):
                    url = inner
                if url:
                    break
        if not url:
            raise SeedrError("Seedr did not return a download URL for this file.")
        return url

    async def _delete(self, item_type: str, item_id: int) -> None:
        import json as _json
        body = await self._call(
            "delete", delete_arr=_json.dumps([{"type": item_type, "id": item_id}])
        )
        rv = body.get("result")
        if rv is False or str(rv).lower() == "false":
            raise SeedrError(f"Could not delete Seedr {item_type} {item_id}")

    async def delete_folder(self, folder_id: int) -> None:
        await self._delete("folder", folder_id)

    async def delete_file(self, file_id: int) -> None:
        await self._delete("file", file_id)

    async def delete_torrent(self, torrent_id: int) -> None:
        await self._delete("torrent", torrent_id)

    async def storage(self) -> tuple[int, int]:
        """(total, used) bytes. (0, 0) if Seedr won't say."""
        try:
            body = await self._call("get_settings")
        except SeedrError:
            body = {}
        sources: list[dict] = [body]
        if isinstance(body.get("account"), dict):
            sources.append(body["account"])
        total = used = 0
        for src in sources:
            for key in ("space_max", "storage_max", "storage_total", "quota"):
                if src.get(key):
                    total = int(src[key])
                    break
            for key in ("space_used", "storage_used", "quota_used"):
                if src.get(key) is not None:
                    used = int(src[key])
                    break
            if total:
                break
        if not total:
            listing = await self.list_folder(0)
            total, used = listing.space_max, listing.space_used
        return total, used

    # ── Completion polling ──────────────────────────────────────────────

    async def wait_ready(
        self,
        *,
        torrent_id: int | None,
        name_hint: str,
        known_folder_ids: set[int],
        timeout_s: float = 4 * 3600,
        interval_s: float = 10.0,
        progress_cb: Callable[[float, str], Awaitable[None]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> SeedrFolderInfo:
        """Poll until the torrent's folder materialises. Returns the folder."""
        deadline = time.monotonic() + timeout_s
        ever_seen = False
        gone_polls = 0
        candidate: tuple[int, Any, int] | None = None  # (id, signature, stable_polls)

        def _match_folder(folders: list[SeedrFolderInfo],
                          torrent_name: str) -> SeedrFolderInfo | None:
            tkey = _name_key(torrent_name or name_hint)
            if not tkey:
                return None
            matches = [
                f for f in folders
                if f.id not in known_folder_ids and (fkey := _name_key(f.name)) and (
                    fkey == tkey
                    or (min(len(fkey), len(tkey)) >= 8
                        and (fkey in tkey or tkey in fkey))
                )
            ]
            return matches[0] if len(matches) == 1 else None

        async def _signature(folder: SeedrFolderInfo) -> Any:
            try:
                listing = await self.list_folder(folder.id)
            except SeedrError:
                return None
            files = tuple(sorted((f.id, f.size) for f in listing.files))
            subs = tuple(sorted((f.id, f.size) for f in listing.folders))
            if not files and not subs:
                return None
            size = max(folder.size,
                       sum(s for _, s in files) + sum(s for _, s in subs))
            return (size, files, subs)

        while time.monotonic() < deadline:
            if cancel_token:
                cancel_token.raise_if_cancelled()
            root = await self.list_folder(0)
            active = None
            if torrent_id is not None:
                active = next((t for t in root.torrents if t.id == torrent_id), None)
            if active is None and torrent_id is None and root.torrents and not ever_seen:
                active = root.torrents[0]

            if active is not None:
                ever_seen = True
                gone_polls = 0
                if progress_cb:
                    await progress_cb(min(active.progress, 99.0), active.name)
                folder = _match_folder(root.folders, active.name)
                if active.progress >= 100.0 and folder:
                    return folder
                # Stuck-meter heuristic: ≥98% + stable folder contents
                if active.progress >= 98.0 and folder:
                    sig = await _signature(folder)
                    if sig is not None:
                        if candidate and candidate[0] == folder.id and candidate[1] == sig:
                            candidate = (folder.id, sig, candidate[2] + 1)
                        else:
                            candidate = (folder.id, sig, 1)
                        size_done = active.size > 0 and sig[0] >= active.size * 0.995
                        if size_done or candidate[2] >= 3:
                            return folder
                    else:
                        candidate = None
            else:
                # Torrent vanished from the transfer list → either finished
                # (folder exists) or failed/removed.
                folder = _match_folder(root.folders, name_hint)
                if folder:
                    if progress_cb:
                        await progress_cb(100.0, folder.name)
                    return folder
                gone_polls += 1
                if ever_seen and gone_polls >= 6:
                    raise SeedrError(
                        "The torrent disappeared from Seedr without producing "
                        "a folder — it may have failed or been removed."
                    )
                if not ever_seen and gone_polls >= 18:
                    raise SeedrError(
                        "Seedr never showed this torrent as downloading — "
                        "it may have been rejected."
                    )
            await asyncio.sleep(interval_s)

        raise SeedrTimeout("Timed out waiting for Seedr to finish this torrent.")
