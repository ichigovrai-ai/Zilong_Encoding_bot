"""Extract streams / plain download for magnet episodes and direct links."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.config import Config
from bot.database.connection import Database
from bot.handlers import wizard
from bot.jobs import worker
from bot.jobs.models import InputType, Job, JobKind, new_job_id
from bot.jobs.repository import JobRepository
from bot.jobs.states import JobStatus
from bot.runtime import Runtime
from bot.ui import keyboards
from bot.ui.callbacks import parse


class _Manager:
    def __init__(self):
        self.enqueued: list[str] = []
        self.upload_sem = __import__("asyncio").Semaphore(1)

    async def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


class _Client:
    def __init__(self):
        self.sent: list[tuple] = []
        self._n = 100

    async def send_message(self, chat_id, text, **kw):
        self._n += 1
        return SimpleNamespace(id=self._n)


@pytest.fixture
async def rt(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    repo = JobRepository(db)
    cfg = Config(api_id=1, api_hash="h", bot_token="t", owner_id=1,
                 work_dir=tmp_path / "work", fc_api_keys=("k1",))
    yield Runtime(config=cfg, db=db, repo=repo, manager=_Manager(),
                  client=_Client())
    await db.close()


def _args(kb) -> list[str]:
    return [parse(b.callback_data).arg for row in kb.inline_keyboard for b in row]


MATCHES = [
    {"video": "Show/[Grp] Show - 01.mkv", "video_id": 11, "video_label":
     "[Grp] Show - 01.mkv", "video_size": 1000, "confidence": "exact"},
    {"video": "Show/[Grp] Show - 02.mkv", "video_id": 12, "video_label":
     "[Grp] Show - 02.mkv", "video_size": 2000, "confidence": "exact"},
]


async def _parent(rt, selected) -> Job:
    job = Job(id=new_job_id(), user_id=7, chat_id=7,
              kind=JobKind.SEEDR_HARDSUB, input_type=InputType.MAGNET,
              input_ref="magnet:?xt=urn:btih:abc", input_name="Show",
              status=JobStatus.CREATED,
              seedr={"account": "", "folder_id": 5, "matches": MATCHES,
                     "selected": selected})
    await rt.repo.insert(job)
    return job


# ── keyboards ─────────────────────────────────────────────────────────────
def _labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def test_direct_link_download_lives_in_the_extract_submenu():
    main = keyboards.action_menu("abcd1234", True, dl=True)
    assert "dl" not in _args(main)                      # main menu stays clean
    assert "📤 Extract / Download" in _labels(main)
    assert "extract" in _args(main)
    sub = keyboards.extract_menu("abcd1234", 3, 1, dl=True)
    assert "dl" in _args(sub) and "exsub" in _args(sub) and "exaud" in _args(sub)
    # files sent through Telegram keep the original wording, no download
    plain = keyboards.action_menu("abcd1234", True)
    assert "📤 Extract streams" in _labels(plain)
    assert "dl" not in _args(keyboards.extract_menu("abcd1234", 3, 1))


def test_episode_menu_has_a_single_tools_button():
    kb = keyboards.episodes_menu("abcd1234", MATCHES, [0])
    args = _args(kb)
    assert "mtools" in args
    assert not {"mxsub", "mxaud", "mdl"} & set(args)    # moved into the submenu
    assert all(len(b.callback_data.encode()) <= 64
               for row in kb.inline_keyboard for b in row)


def test_magnet_tools_submenu():
    kb = keyboards.magnet_tools_menu("abcd1234")
    assert {"mxsub", "mxaud", "mdl", "mback"} <= set(_args(kb))
    assert "x" in [parse(b.callback_data).verb
                   for row in kb.inline_keyboard for b in row]


# ── spawning children ─────────────────────────────────────────────────────
async def test_spawn_extract_children_for_selected_episodes(rt):
    parent = await _parent(rt, selected=[1])
    n = await wizard.spawn_seedr_tasks(
        rt, parent, JobKind.EXTRACT,
        {"extract_target": "subtitles", "extract_streams": "all"})
    assert n == 1 and len(rt.manager.enqueued) == 1
    child = await rt.repo.get(rt.manager.enqueued[0])
    assert child.kind == JobKind.EXTRACT
    assert child.input_type == InputType.SEEDR_FILE and child.input_ref == "12"
    assert child.options["extract_target"] == "subtitles"
    assert child.seedr["parent"] == parent.id and child.seedr["folder_id"] == 5
    assert (await rt.repo.get(parent.id)).status == JobStatus.COMPLETED


async def test_spawn_download_children_for_all_selected(rt):
    parent = await _parent(rt, selected=[0, 1])
    n = await wizard.spawn_seedr_tasks(rt, parent, JobKind.DOWNLOAD, {})
    assert n == 2
    kinds = {(await rt.repo.get(j)).kind for j in rt.manager.enqueued}
    assert kinds == {JobKind.DOWNLOAD}


async def test_spawn_nothing_when_no_selection(rt):
    parent = await _parent(rt, selected=[])
    assert await wizard.spawn_seedr_tasks(rt, parent, JobKind.DOWNLOAD, {}) == 0
    assert (await rt.repo.get(parent.id)).status != JobStatus.COMPLETED


# ── worker helpers ────────────────────────────────────────────────────────
def test_episode_prefix_only_for_batch_children():
    batch = Job(id="a", user_id=1, chat_id=1, kind=JobKind.EXTRACT,
                input_name="[Grp] Show - 02.mkv", seedr={"parent": "p"})
    single = Job(id="b", user_id=1, chat_id=1, kind=JobKind.EXTRACT,
                 input_name="[Grp] Show - 02.mkv")
    assert worker._episode_prefix(batch) == "E02_"
    assert worker._episode_prefix(single) == ""
    s01 = Job(id="c", user_id=1, chat_id=1, kind=JobKind.EXTRACT,
              input_name="Show.S01E05.mkv", seedr={"parent": "p"})
    assert worker._episode_prefix(s01) == "S01E05_"


async def test_run_download_sends_file_untouched(rt, monkeypatch, tmp_path):
    uploaded = {}

    async def fake_upload(client, chat_id, path, **kw):
        uploaded.update(path=Path(path), name=Path(path).name,
                        data=Path(path).read_bytes(), as_video=kw["as_video"])

    class _Rep:
        async def update(self, *a, **k): ...

    monkeypatch.setattr(worker, "upload_file", fake_upload)
    monkeypatch.setattr(worker, "cleanup_job_dir", None, raising=False)

    async def _noop(*a, **k): ...
    monkeypatch.setattr(worker, "_maybe_delete_seedr_folder", _noop)
    import bot.services.cleanup as cleanup
    monkeypatch.setattr(cleanup, "cleanup_job_dir", _noop)

    job = Job(id=new_job_id(), user_id=7, chat_id=7, kind=JobKind.DOWNLOAD,
              input_type=InputType.URL, input_ref="https://x/y.zip",
              input_name="archive.zip")
    await rt.repo.insert(job)
    # The pipeline has already moved the job to DOWNLOADING when it gets here.
    await rt.repo.transition(job.id, JobStatus.DOWNLOADING, stage="downloading")
    work = tmp_path / "w"
    work.mkdir()
    src = work / "input.bin"
    src.write_bytes(b"PK-not-a-video")

    updates = []
    reporter = SimpleNamespace(update=lambda *a, **k: _coro(updates.append(a)))
    token = SimpleNamespace(cancelled=False, raise_if_cancelled=lambda: None)

    await worker._run_download(rt, job, src, work, token, reporter,
                               0.0, "A", "auto")

    assert uploaded["name"] == "archive.zip"           # original name kept
    assert uploaded["data"] == b"PK-not-a-video"       # bytes untouched
    assert uploaded["as_video"] is False                # not a video → document
    assert (await rt.repo.get(job.id)).status == JobStatus.COMPLETED


async def _coro(_):
    return None


# ── button clicks (callback handlers) ─────────────────────────────────────
from bot.handlers import callbacks


class _CB:
    """Minimal CallbackQuery stand-in recording what the bot answers/edits."""
    def __init__(self):
        self.answers: list[tuple] = []
        self.edits: list[tuple] = []
        outer = self

        class _Msg:
            async def edit_text(self, text, reply_markup=None, **kw):
                outer.edits.append((text, reply_markup))
        self.message = _Msg()

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


async def test_click_tools_button_opens_submenu_then_back(rt):
    parent = await _parent(rt, selected=[0, 1])
    parent.stage = "select_episodes"
    cb = _CB()
    await callbacks._handle_action(rt, cb, parent, "mtools")
    text, kb = cb.edits[-1]
    assert "2" in text and {"mxsub", "mxaud", "mdl", "mback"} <= set(_args(kb))

    await callbacks._handle_action(rt, cb, parent, "mback")
    text, kb = cb.edits[-1]
    assert "TORRENT READY" in text and "mtools" in _args(kb)


async def test_click_tools_without_selection_is_refused(rt):
    parent = await _parent(rt, selected=[])
    cb = _CB()
    await callbacks._handle_action(rt, cb, parent, "mtools")
    assert cb.answers[-1][1] is True and not cb.edits      # alert, no menu


async def test_click_magnet_download_spawns_and_confirms(rt):
    parent = await _parent(rt, selected=[0, 1])
    cb = _CB()
    await callbacks._handle_action(rt, cb, parent, "mdl")
    assert len(rt.manager.enqueued) == 2
    assert "Queued 2 episode(s)" in cb.edits[-1][0]


async def test_click_download_on_direct_link_enqueues(rt):
    job = Job(id=new_job_id(), user_id=7, chat_id=7, kind=JobKind.ENCODE,
              input_type=InputType.URL, input_ref="https://x/v.mkv",
              input_name="v.mkv")
    await rt.repo.insert(job)
    cb = _CB()
    await callbacks._handle_action(rt, cb, job, "dl")
    assert rt.manager.enqueued == [job.id]
    assert (await rt.repo.get(job.id)).kind == JobKind.DOWNLOAD


async def test_click_download_refused_for_telegram_files(rt):
    job = Job(id=new_job_id(), user_id=7, chat_id=7, kind=JobKind.ENCODE,
              input_type=InputType.TG_VIDEO, input_ref="x", input_name="v.mkv")
    await rt.repo.insert(job)
    cb = _CB()
    await callbacks._handle_action(rt, cb, job, "dl")
    assert not rt.manager.enqueued and cb.answers[-1][1] is True
