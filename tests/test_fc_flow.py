"""FreeConvert flows: menus, wizard steps (direct link + Seedr magnet),
config keys and the ported house style / fonts."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.config import Config
from bot.database.connection import Database
from bot.handlers import wizard
from bot.jobs.models import InputType, Job, JobKind, new_job_id
from bot.jobs.repository import JobRepository
from bot.jobs.states import JobStatus
from bot.runtime import Runtime
from bot.services import house_style
from bot.ui import keyboards
from bot.ui.callbacks import parse


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(api_id=1, api_hash="h", bot_token="t", owner_id=1,
                  work_dir=tmp_path / "work", fc_api_keys=("k1", "k2"), **kw)


class FakeManager:
    def __init__(self, repo):
        self.repo = repo
        self.enqueued: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        await self.repo.transition(job_id, JobStatus.QUEUED, stage="queued")
        self.enqueued.append(job_id)


class FakeClient:
    def __init__(self):
        self._n = 0

    async def send_message(self, chat_id, text, **kw):
        self._n += 1
        return SimpleNamespace(id=1000 + self._n)


@pytest.fixture
async def rt(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    repo = JobRepository(db)
    yield Runtime(config=_cfg(tmp_path), db=db, repo=repo,
                  manager=FakeManager(repo), client=FakeClient())
    await db.close()


def _url_job(**kw) -> Job:
    d = dict(id=new_job_id(), user_id=7, chat_id=7, kind=JobKind.ENCODE,
             input_type=InputType.URL, input_ref="https://cdn.example/ep.mkv",
             input_name="ep.mkv")
    d.update(kw)
    return Job(**d)


# ── config ──────────────────────────────────────────────────────────────

def test_config_fc_keys(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.fc_enabled and cfg.fc_keys_csv == "k1,k2"
    assert not Config(api_id=1, api_hash="h", bot_token="t", owner_id=1).fc_enabled


# ── keyboards ───────────────────────────────────────────────────────────

def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_action_menu_fc_button_only_when_enabled():
    assert "z|a|abcd1234|fchs" not in _cbs(keyboards.action_menu("abcd1234", False))
    assert "z|a|abcd1234|fchs" in _cbs(keyboards.action_menu("abcd1234", False, fc=True))


def test_quality_menu_has_all_choices_and_valid_callbacks():
    labels = [b.text for row in keyboards.fc_quality_menu("abcd1234").inline_keyboard
              for b in row]
    for wanted in ("360p · YouTube", "480p · YouTube", "720p · Facebook",
                   "1080p", "Original quality"):
        assert wanted in labels
    for data in _cbs(keyboards.fc_quality_menu("abcd1234")):
        assert parse(data) is not None


def test_style_menu_offers_a_b_c_d_and_original():
    args = {parse(d).arg for d in _cbs(keyboards.fc_style_menu("abcd1234"))
            if parse(d).verb == "fy"}
    assert args == {"a", "b", "c", "d", "orig"}


def test_episodes_menu_fc_button():
    m = [{"video_label": "e1.mkv", "video": "e1.mkv", "confidence": "exact"}]
    assert "z|a|abcd1234|fcmag" not in _cbs(keyboards.episodes_menu("abcd1234", m, [0]))
    assert "z|a|abcd1234|fcmag" in _cbs(keyboards.episodes_menu("abcd1234", m, [0], fc=True))


# ── direct link: quality → style → subtitle → starts ────────────────────

async def test_direct_link_flow(rt):
    job = _url_job()
    await rt.repo.insert(job)

    text, kb = await wizard.enter_kind(rt, job, JobKind.FC_HARDSUB)
    assert job.options["step"] == "fcq" and "quality" in text
    assert job.options["engine"] == "fc"

    _, _, alert = await wizard.handle_selection(rt, job, "fq", "720")
    assert alert is None and job.options["fc_res_sel"] == ["720"]
    assert job.options["step"] == "fcq"          # ticking doesn't advance
    _, _, alert = await wizard.handle_selection(rt, job, "fq", "done")
    assert alert is None and job.options["fc_res"] == 720
    assert job.options["fc_res_list"] == [720]
    assert job.options["step"] == "fcy"

    text, _, alert = await wizard.handle_selection(rt, job, "fy", "b")
    assert alert is None and job.options["fc_style"] == "b"
    assert job.options["step"] == "fcsub" and job.options["await"] == "subtitle"
    assert "subtitle" in text.lower()

    job.options["subtitle_file"] = "/tmp/x.ass"
    text, _ = await wizard.after_subtitle_upload(rt, job)
    assert rt.manager.enqueued == [job.id]
    assert (await rt.repo.get(job.id)).status == JobStatus.QUEUED
    assert "Queued" in text
    assert "await" not in job.options and job.options["subtitle_source"] == "upload"


async def test_original_quality_and_orig_style_accepted(rt):
    job = _url_job()
    await rt.repo.insert(job)
    await wizard.enter_kind(rt, job, JobKind.FC_HARDSUB)
    await wizard.handle_selection(rt, job, "fq", "0")
    await wizard.handle_selection(rt, job, "fq", "done")
    assert job.options["fc_res"] == 0
    await wizard.handle_selection(rt, job, "fy", "orig")
    assert job.options["fc_style"] == "orig"


async def test_bad_args_are_rejected(rt):
    job = _url_job()
    await rt.repo.insert(job)
    await wizard.enter_kind(rt, job, JobKind.FC_HARDSUB)
    _, _, alert = await wizard.handle_selection(rt, job, "fq", "999")
    assert alert and job.options["step"] == "fcq"
    await wizard.handle_selection(rt, job, "fq", "480")
    await wizard.handle_selection(rt, job, "fq", "done")
    _, _, alert = await wizard.handle_selection(rt, job, "fy", "z")
    assert alert and job.options["step"] == "fcy"


async def test_back_goes_to_previous_step(rt):
    job = _url_job()
    await rt.repo.insert(job)
    await wizard.enter_kind(rt, job, JobKind.FC_HARDSUB)
    await wizard.handle_selection(rt, job, "fq", "480")
    await wizard.handle_selection(rt, job, "fq", "done")
    await wizard.handle_selection(rt, job, "bk", "")
    assert job.options["step"] == "fcq"


# ── Seedr magnet: quality → style → children (no subtitle asked) ────────

def _seedr_parent() -> Job:
    return Job(
        id=new_job_id(), user_id=7, chat_id=7, kind=JobKind.SEEDR_HARDSUB,
        input_type=InputType.MAGNET, input_ref="magnet:?xt=urn:btih:abc",
        input_name="Show", status=JobStatus.WAITING_FOR_INPUT,
        seedr={
            "account": "", "folder_id": 5, "folder_name": "Show",
            "selected": [0, 1],
            "matches": [
                {"video_id": 11, "video_label": "Show S01E01 1080p.mkv",
                 "video": "Show S01E01 1080p.mkv", "video_size": 5,
                 "sub_id": 21, "subtitle": "subs/e1.ass", "confidence": "exact"},
                {"video_id": 12, "video_label": "Show S01E02 1080p.mkv",
                 "video": "Show S01E02 1080p.mkv", "video_size": 5,
                 "sub_id": None, "subtitle": None, "confidence": "none"},
            ],
        },
    )


async def test_magnet_flow_spawns_fc_children(rt):
    parent = _seedr_parent()
    await rt.repo.insert(parent)

    text, _ = await wizard.enter_fc_magnet(rt, parent)
    assert parent.options["step"] == "fcq"
    await wizard.handle_selection(rt, parent, "fq", "480")
    await wizard.handle_selection(rt, parent, "fq", "done")
    assert parent.options["step"] == "fcy"
    text, kb, alert = await wizard.handle_selection(rt, parent, "fy", "c")

    assert alert is None and kb is None and "Queued 2" in text
    assert (await rt.repo.get(parent.id)).status == JobStatus.COMPLETED
    assert len(rt.manager.enqueued) == 2
    kids = [await rt.repo.get(j) for j in rt.manager.enqueued]
    assert all(k.kind == JobKind.FC_HARDSUB and k.input_type == InputType.SEEDR_FILE
               for k in kids)
    assert all(k.options["fc_res"] == 480 and k.options["fc_style"] == "c"
               for k in kids)
    # matched external sub is used; the other episode falls back to the
    # video's own French track
    assert kids[0].options["subtitle_source"] == "seedr"
    assert kids[1].options["subtitle_source"] == "embedded_auto"


async def test_magnet_back_from_first_step_returns_to_episode_picker(rt):
    parent = _seedr_parent()
    await rt.repo.insert(parent)
    await wizard.enter_fc_magnet(rt, parent)
    text, kb, alert = await wizard.handle_selection(rt, parent, "bk", "")
    assert alert is None and "fc_mag" not in parent.options
    assert "z|a|%s|fcmag" % parent.id in _cbs(kb)


# ── ported house style + fonts ──────────────────────────────────────────

_ASS = """[Script Info]
PlayResX: 640
PlayResY: 360

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BottomCenter,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,BottomCenter,,0,0,0,,Bonjour
"""


@pytest.mark.parametrize("key", ["a", "b", "c", "d", "orig"])
def test_every_style_renders(tmp_path, key):
    src = tmp_path / "in.ass"
    src.write_text(_ASS, encoding="utf-8")
    out = house_style.apply_hardsub_style(str(src), str(tmp_path / "o.ass"), style_key=key)
    text = Path(out).read_text(encoding="utf-8")
    assert "Bonjour" in text
    assert ("[Fonts]" in text) == (key == "c")   # only Gandhi Sans is embedded


def test_gandhi_sans_files_are_found():
    for name in house_style.STYLE_FONT_FILES["c"]:
        assert (Path(house_style.FONTS_DIR) / name).is_file(), name
