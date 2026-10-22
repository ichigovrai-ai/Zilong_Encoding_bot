"""FreeConvert multi-quality: tick several qualities, Done, one FC run."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.config import Config
from bot.database.connection import Database
from bot.handlers import wizard
from bot.jobs import fc_pipeline
from bot.jobs.fc_pipeline import fc_res_list
from bot.jobs.models import InputType, Job, JobKind, new_job_id
from bot.jobs.repository import JobRepository
from bot.jobs.states import JobStatus
from bot.runtime import Runtime
from bot.services import freeconvert
from bot.ui import keyboards
from bot.ui.callbacks import parse
from bot.utils.subprocesses import CancelToken


def _labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _args(kb) -> list[str]:
    return [parse(b.callback_data).arg for row in kb.inline_keyboard for b in row]


# ── keyboard ──────────────────────────────────────────────────────────────
def test_menu_without_selection_has_no_done_button():
    kb = keyboards.fc_quality_menu("abcd1234")
    assert "done" not in _args(kb)
    assert not any(l.startswith("✅") for l in _labels(kb))


def test_menu_marks_selected_and_shows_done():
    kb = keyboards.fc_quality_menu("abcd1234", ["720", "480"])
    labels = _labels(kb)
    assert "✅ 720p · Facebook" in labels and "✅ 480p · YouTube" in labels
    assert "360p · YouTube" in labels                       # not ticked
    assert "✔️ Done (2)" in labels and "done" in _args(kb)
    assert all(len(b.callback_data.encode()) <= 64
               for row in kb.inline_keyboard for b in row)


# ── wizard ────────────────────────────────────────────────────────────────
@pytest.fixture
async def rt(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    repo = JobRepository(db)
    cfg = Config(api_id=1, api_hash="h", bot_token="t", owner_id=1,
                 work_dir=tmp_path / "work", fc_api_keys=("k1",))

    class _M:
        enqueued: list = []
        async def enqueue(self, j): self.enqueued.append(j)

    yield Runtime(config=cfg, db=db, repo=repo, manager=_M(),
                  client=SimpleNamespace())
    await db.close()


async def _fc_job(rt) -> Job:
    job = Job(id=new_job_id(), user_id=7, chat_id=7, kind=JobKind.ENCODE,
              input_type=InputType.URL, input_ref="https://x/v.mkv",
              input_name="v.mkv")
    await rt.repo.insert(job)
    await wizard.enter_kind(rt, job, JobKind.FC_HARDSUB)
    return job


async def test_tick_untick_and_done(rt):
    job = await _fc_job(rt)
    sel = lambda: job.options.get("fc_res_sel")
    await wizard.handle_selection(rt, job, "fq", "720")
    await wizard.handle_selection(rt, job, "fq", "480")
    assert sel() == ["720", "480"] and job.options["step"] == "fcq"
    await wizard.handle_selection(rt, job, "fq", "720")          # untick
    assert sel() == ["480"]
    await wizard.handle_selection(rt, job, "fq", "720")
    await wizard.handle_selection(rt, job, "fq", "done")
    assert job.options["fc_res_list"] == [480, 720]              # order picked
    assert job.options["fc_res"] == 480 and job.options["step"] == "fcy"


async def test_done_requires_a_selection(rt):
    job = await _fc_job(rt)
    _, _, alert = await wizard.handle_selection(rt, job, "fq", "done")
    assert alert and job.options["step"] == "fcq"


async def test_at_most_three_qualities(rt):
    job = await _fc_job(rt)
    for q in ("360", "480", "720"):
        _, _, alert = await wizard.handle_selection(rt, job, "fq", q)
        assert alert is None
    _, _, alert = await wizard.handle_selection(rt, job, "fq", "1080")
    assert alert and "Maximum 3" in alert
    assert job.options["fc_res_sel"] == ["360", "480", "720"]


async def test_selection_is_shown_in_the_step_text(rt):
    job = await _fc_job(rt)
    text, kb, _ = await wizard.handle_selection(rt, job, "fq", "720")
    assert "1 selected" in text and "✔️ Done (1)" in _labels(kb)


# ── helpers ───────────────────────────────────────────────────────────────
def test_fc_res_list_fallbacks():
    assert fc_res_list({"fc_res": 480}) == [480]
    assert fc_res_list({}) == [0]
    assert fc_res_list({"fc_res_list": [720, 480, 720], "fc_res": 1}) == [720, 480]
    assert fc_res_list({"fc_res_list": [1, 2, 3, 4, 5]}) == [1, 2, 3]
    assert fc_res_list({"fc_res_list": ["x", 360]}) == [360]


def test_settings_line_lists_all_qualities():
    j = Job(id="a", user_id=1, chat_id=1, kind=JobKind.FC_HARDSUB,
            options={"fc_res_list": [720, 0], "fc_style": "b"})
    line = fc_pipeline._settings_line(j)
    assert line.startswith("FreeConvert · 720p + Original · Style B")
    assert "720p" in fc_pipeline._settings_line(j, 720)
    single = Job(id="b", user_id=1, chat_id=1, kind=JobKind.FC_HARDSUB,
                 options={"fc_res": 720, "fc_style": "b"})
    assert "720p · Facebook" in fc_pipeline._settings_line(single)


# ── one FreeConvert payload for all qualities ─────────────────────────────
def test_multi_payload_shares_one_import_and_one_subtitle():
    names = {"720p": "A 720p.mp4", "orig": "A.mp4"}
    p = freeconvert._create_hardsub_multi_payload(
        video_url="https://cdn/x.mkv", input_format="mkv", output_format="mp4",
        qualities={"720p": (1280, 720), "orig": None},
        output_filenames=names, subtitle_b64="U1VC", crf=23, speed="medium")
    tasks = p["tasks"]
    assert [k for k, t in tasks.items() if t["operation"] == "import/url"] == ["import-video"]
    assert set(tasks) == {"import-video", "convert_720p", "export_720p",
                          "convert_orig", "export_orig"}
    assert tasks["convert_720p"]["input"] == "import-video"
    assert tasks["export_720p"]["input"] == ["convert_720p"]
    o720, oorig = tasks["convert_720p"]["options"], tasks["convert_orig"]["options"]
    assert o720["video_screen_size"] == "1280:720"
    assert "video_screen_size" not in oorig                       # original size
    assert o720["subtitle"] == oorig["subtitle"] == "U1VC"        # same subtitle
    assert o720["subtitle_mode"] == "hard"
    assert tasks["convert_orig"]["filename"] == "A.mp4"


# ── pipeline: ONE FreeConvert call → several files → several uploads ──────
from tests.test_fc_pipeline import _ASS, _queued_job, _run, env  # noqa: F401


async def test_multi_quality_run_uses_one_fc_job_and_uploads_each(env):
    seen: dict = {"multi": [], "single": []}

    async def fake_multi(api_keys, video_url, source_name, subtitle_path,
                         dest_dir, *, qualities, **kw):
        seen["multi"].append({"url": video_url, "qualities": dict(qualities),
                              "sub": subtitle_path, **kw})
        out = {}
        for label in qualities:
            f = Path(dest_dir) / f"Show EP01 {label}.mp4"
            f.write_bytes(b"\0" * 2048)
            out[label] = str(f)
        return out

    async def no_single(*a, **k):
        seen["single"].append(1)
        raise AssertionError("single-quality path must not run")

    env.monkeypatch.setattr(fc_pipeline.freeconvert,
                            "hardsub_remote_url_multi", fake_multi)
    env.monkeypatch.setattr(fc_pipeline.freeconvert,
                            "hardsub_remote_url", no_single)

    job = await _queued_job(env, source="upload", fc_res=720,
                            fc_res_list=[720, 480, 0])
    await _run(env, job)

    (call,) = seen["multi"]                       # a single FreeConvert job
    assert call["url"] == "https://cdn.example/Show.S01E01.mkv"
    assert call["qualities"] == {"720p": (1280, 720), "480p": (854, 480),
                                 "orig": None}
    assert call["style_key"] == "b" and call["codec"] == "h264"
    assert not seen["single"]
    uploaded = [Path(p).name for p, _ in env.calls["uploads"]]
    assert uploaded == ["Show EP01 720p.mp4", "Show EP01 480p.mp4",
                        "Show EP01 orig.mp4"]       # every quality delivered
    assert (await env.rt.repo.get(job.id)).status == JobStatus.COMPLETED


async def test_multi_quality_skips_a_bad_output_but_sends_the_rest(env):
    async def fake_multi(api_keys, video_url, source_name, subtitle_path,
                         dest_dir, *, qualities, **kw):
        out = {}
        for i, label in enumerate(qualities):
            f = Path(dest_dir) / f"Show {label}.mp4"
            f.write_bytes(b"" if i == 0 else b"\0" * 2048)   # first one is empty
            out[label] = str(f)
        return out

    env.monkeypatch.setattr(fc_pipeline.freeconvert,
                            "hardsub_remote_url_multi", fake_multi)
    job = await _queued_job(env, source="upload", fc_res=720,
                            fc_res_list=[720, 480])
    reporter = SimpleNamespace(texts=[], update=None)

    async def _upd(text, reply_markup=None, force=False):
        reporter.texts.append(text)
    reporter.update = _upd

    job = await env.rt.repo.get(job.id)
    await fc_pipeline.run_fc_job(env.rt, job, CancelToken(), reporter, 0.0,
                                 {}, "A")
    assert len(env.calls["uploads"]) == 1                      # 480p only
    assert any("Skipped" in t and "720p" in t for t in reporter.texts)
    assert (await env.rt.repo.get(job.id)).status == JobStatus.COMPLETED


async def test_single_quality_run_still_uses_the_plain_path(env):
    job = await _queued_job(env, source="upload", fc_res=480,
                            fc_res_list=[480])
    await _run(env, job)
    (fc,) = env.calls["fc"]                                    # original fake
    assert fc["resize"] == (854, 480)
    assert len(env.calls["uploads"]) == 1
