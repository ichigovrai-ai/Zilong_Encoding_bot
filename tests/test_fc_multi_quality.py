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
_real_wait = freeconvert._wait_for_job
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


# ── a quality FreeConvert doesn't deliver is retried, then reported ───────
def _fc_job_response(missing: str | None = None) -> dict:
    tasks = []
    for q in ("360p", "720p"):
        tasks.append({"name": f"convert_{q}", "status": "completed"})
        if q == missing:
            tasks[-1] = {"name": f"convert_{q}", "status": "failed",
                         "message": "Out of conversion minutes"}
            tasks.append({"name": f"export_{q}", "status": "pending",
                          "result": {}})
        else:
            tasks.append({"name": f"export_{q}", "status": "completed",
                          "result": {"url": f"https://fc/{q}.mp4"}})
    return {"id": "job1", "status": "completed", "tasks": tasks}


@pytest.fixture
def fc_net(monkeypatch, tmp_path):
    """Replace every network/FS step of hardsub_remote_url_multi."""
    sub = tmp_path / "sub.ass"
    sub.write_text("[Script Info]\n")
    state = SimpleNamespace(job=_fc_job_response(), single_calls=[],
                            single_error=None, sub=str(sub), dest=str(tmp_path),
                            events=[])

    async def _key(keys): return "k"
    async def _codec(api_key, codec, cfg, fmt): return "h264", {}, 128
    async def _post(api_key, payload): return {"id": "job1"}
    async def _wait(api_key, job_id, cb, **kw): return state.job

    async def _download(url, dest, cb=None):
        state.events.append(("download", url))
        Path(dest).write_bytes(b"x" * 10)
        return dest

    async def _single(api_keys, video_url, source_name, sub_path, dest_dir, **kw):
        state.single_calls.append(kw["resize"])
        if state.single_error:
            raise state.single_error
        if kw.get("url_cb"):
            await kw["url_cb"]("https://fc/retry.mp4")
        out = Path(dest_dir) / f"retry_{kw['resize'][1]}p.mp4"
        out.write_bytes(b"y" * 10)
        return str(out)

    monkeypatch.setattr(freeconvert, "pick_working_key", _key)
    monkeypatch.setattr(freeconvert, "_codec_setup", _codec)
    monkeypatch.setattr(freeconvert, "_post_job", _post)
    monkeypatch.setattr(freeconvert, "_wait_for_job", _wait)
    monkeypatch.setattr(freeconvert, "_download_file", _download)
    monkeypatch.setattr(freeconvert, "hardsub_remote_url", _single)
    monkeypatch.setattr(freeconvert, "merge_fc_video_options",
                        lambda *a, **k: None)
    return state


async def _call(st, failures=None, url_cb=None):
    return await freeconvert.hardsub_remote_url_multi(
        "key", "https://cdn/ep.mkv", "Show - 12.mkv", st.sub, st.dest,
        qualities={"360p": (640, 360), "720p": (1280, 720)},
        failures=failures, url_cb=url_cb)


async def test_all_qualities_delivered_by_the_grouped_job(fc_net):
    out = await _call(fc_net)
    assert set(out) == {"360p", "720p"} and not fc_net.single_calls


async def test_missing_quality_is_retried_as_its_own_job(fc_net):
    fc_net.job = _fc_job_response(missing="720p")
    failures: dict[str, str] = {}
    out = await _call(fc_net, failures)
    assert set(out) == {"360p", "720p"}              # nothing lost
    assert fc_net.single_calls == [(1280, 720)]      # only the missing one
    assert failures == {}


async def test_failure_reason_is_reported_when_the_retry_fails_too(fc_net):
    fc_net.job = _fc_job_response(missing="720p")
    fc_net.single_error = RuntimeError("FreeConvert quota exceeded")
    failures: dict[str, str] = {}
    out = await _call(fc_net, failures)
    assert set(out) == {"360p"}                       # the good one is kept
    reason = failures["720p"]
    assert "convert_720p failed" in reason and "Out of conversion minutes" in reason
    assert "quota exceeded" in reason


async def test_nothing_exported_at_all_raises(fc_net):
    fc_net.job = {"id": "j", "status": "completed", "tasks": []}
    fc_net.single_error = RuntimeError("nope")
    with pytest.raises(RuntimeError):
        await _call(fc_net, {})


async def test_pipeline_shows_why_a_quality_is_missing(env):
    async def fake_multi(api_keys, video_url, source_name, subtitle_path,
                         dest_dir, *, qualities, failures=None, **kw):
        f = Path(dest_dir) / "Show 360p.mp4"
        f.write_bytes(b"\0" * 2048)
        failures["720p"] = "convert_720p failed (Out of conversion minutes)"
        return {"360p": str(f)}

    env.monkeypatch.setattr(fc_pipeline.freeconvert,
                            "hardsub_remote_url_multi", fake_multi)
    job = await _queued_job(env, source="upload", fc_res=360,
                            fc_res_list=[360, 720])
    texts: list[str] = []

    async def _upd(text, reply_markup=None, force=False):
        texts.append(text)

    job = await env.rt.repo.get(job.id)
    await fc_pipeline.run_fc_job(env.rt, job, CancelToken(),
                                 SimpleNamespace(update=_upd), 0.0, {}, "A")
    assert len(env.calls["uploads"]) == 1
    final = next(t for t in reversed(texts) if "COMPLETED" in t)
    assert "Skipped" in final and "720p" in final and "Out of conversion" in final


# ── optional FreeConvert download link, sent before the download ──────────
async def test_links_are_handed_over_before_any_download(fc_net):
    async def url_cb(quality, url):
        fc_net.events.append(("link", quality, url))

    await _call(fc_net, url_cb=url_cb)
    kinds = [e[0] for e in fc_net.events]
    assert kinds == ["link", "link", "download", "download"]   # links first
    assert fc_net.events[0][1:] == ("360p", "https://fc/360p.mp4")
    assert fc_net.events[1][1:] == ("720p", "https://fc/720p.mp4")


async def test_a_failing_link_callback_never_blocks_the_download(fc_net):
    async def url_cb(quality, url):
        raise RuntimeError("telegram is down")

    out = await _call(fc_net, url_cb=url_cb)
    assert set(out) == {"360p", "720p"}


async def test_retried_quality_gets_its_link_too(fc_net):
    fc_net.job = _fc_job_response(missing="720p")
    got = []

    async def url_cb(quality, url):
        got.append((quality, url))

    await _call(fc_net, url_cb=url_cb)
    assert ("360p", "https://fc/360p.mp4") in got
    assert ("720p", "https://fc/retry.mp4") in got            # labelled with its quality


def test_fc_link_setting_is_opt_in():
    from bot.services.user_settings import button_label, next_value, value_of
    assert value_of({}, "fc_link") == "off"                    # off by default
    assert next_value({}, "fc_link") == "on"
    assert "FC link" in button_label({}, "fc_link")
    assert value_of({"fc_link": "on"}, "fc_link") == "on"


class _Chat:
    def __init__(self, fail=False):
        self.messages: list[str] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("flood")
        self.messages.append(text)


async def _run_with_link_setting(env, settings, client, multi=True):
    import dataclasses
    rt = dataclasses.replace(env.rt, client=client)

    async def fake_multi(api_keys, video_url, source_name, subtitle_path,
                         dest_dir, *, qualities, url_cb=None, **kw):
        if url_cb:
            for label in qualities:
                await url_cb(label, f"https://fc/{label}.mp4?sig=a&x=1")
        out = {}
        for label in qualities:
            f = Path(dest_dir) / f"Show {label}.mp4"
            f.write_bytes(b"\0" * 2048)
            out[label] = str(f)
        return out

    async def fake_single(api_keys, video_url, source_name, subtitle_path,
                          dest_dir, *, url_cb=None, **kw):
        if url_cb:
            await url_cb("https://fc/single.mp4")
        f = Path(dest_dir) / "Show.mp4"
        f.write_bytes(b"\0" * 2048)
        return str(f)

    env.monkeypatch.setattr(fc_pipeline.freeconvert, "hardsub_remote_url_multi", fake_multi)
    env.monkeypatch.setattr(fc_pipeline.freeconvert, "hardsub_remote_url", fake_single)
    job = await _queued_job(env, source="upload", fc_res=720,
                            fc_res_list=[720, 480] if multi else [720])
    job = await env.rt.repo.get(job.id)

    async def _upd(text, reply_markup=None, force=False):
        return None

    await fc_pipeline.run_fc_job(rt, job, CancelToken(),
                                 SimpleNamespace(update=_upd), 0.0, settings, "A")
    return rt, job


async def test_pipeline_sends_one_link_per_quality_when_enabled(env):
    chat = _Chat()
    await _run_with_link_setting(env, {"fc_link": "on"}, chat)
    assert len(chat.messages) == 2
    assert "720p" in chat.messages[0] and "480p" in chat.messages[1]
    assert 'href="https://fc/720p.mp4?sig=a&amp;x=1"' in chat.messages[0]   # escaped
    assert "Temporary link" in chat.messages[0]
    assert len(env.calls["uploads"]) == 2                      # uploads unaffected


async def test_pipeline_sends_the_link_for_a_single_quality(env):
    chat = _Chat()
    await _run_with_link_setting(env, {"fc_link": "on"}, chat, multi=False)
    assert len(chat.messages) == 1
    assert "https://fc/single.mp4" in chat.messages[0]
    assert "·" not in chat.messages[0].split("\n")[0]          # no quality label


async def test_pipeline_sends_no_link_by_default(env):
    chat = _Chat()
    await _run_with_link_setting(env, {}, chat)
    assert chat.messages == []
    assert len(env.calls["uploads"]) == 2


async def test_pipeline_sends_no_link_when_the_setting_is_off(env):
    chat = _Chat()
    await _run_with_link_setting(env, {"fc_link": "off"}, chat, multi=False)
    assert chat.messages == []
    assert len(env.calls["uploads"]) == 1


async def test_a_failing_link_message_never_fails_the_job(env):
    rt, job = await _run_with_link_setting(env, {"fc_link": "on"},
                                           _Chat(fail=True))
    assert len(env.calls["uploads"]) == 2
    assert (await env.rt.repo.get(job.id)).status == JobStatus.COMPLETED


# ── FreeConvert says "completed" while a quality is still compressing ─────
def _task(name, status, url=None, message=""):
    t = {"name": name, "status": status, "result": {}}
    if url:
        t["result"] = {"url": url}
    if message:
        t["message"] = message
    return t


def _job(status, *tasks):
    return {"id": "job1", "status": status, "tasks": list(tasks)}


class _Polls:
    """Feeds _wait_for_job a scripted sequence of job snapshots."""
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    async def __call__(self, api_key, job_id):
        self.calls += 1
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


@pytest.fixture
def no_sleep(monkeypatch):
    async def _s(_):
        return None
    monkeypatch.setattr(freeconvert.asyncio, "sleep", _s)


async def test_wait_keeps_polling_while_another_quality_still_compresses(
        monkeypatch, no_sleep):
    # The exact situation from the logs: job "completed", but orig still running.
    early = _job("completed",
                 _task("import-video", "completed"),
                 _task("convert_360p", "completed"),
                 _task("export_360p", "completed", "https://fc/360.mp4"),
                 _task("convert_orig", "processing"),
                 _task("export_orig", "created"))
    final = _job("completed",
                 _task("import-video", "completed"),
                 _task("convert_360p", "completed"),
                 _task("export_360p", "completed", "https://fc/360.mp4"),
                 _task("convert_orig", "completed"),
                 _task("export_orig", "completed", "https://fc/orig.mp4"))
    polls = _Polls(early, early, final)
    monkeypatch.setattr(freeconvert, "_job_status", polls)
    job = await freeconvert._wait_for_job(
        "k", "job1",
        ready=lambda j: freeconvert._multi_ready(j, ["360p", "orig"]))
    assert polls.calls == 3                               # waited, didn't give up
    assert freeconvert._task_export_url(
        freeconvert._find_task(job, "export_orig")) == "https://fc/orig.mp4"


async def test_wait_without_predicate_is_unchanged(monkeypatch, no_sleep):
    done = _job("completed", _task("export", "completed", "https://fc/x.mp4"))
    monkeypatch.setattr(freeconvert, "_job_status", _Polls(done))
    assert (await freeconvert._wait_for_job("k", "j")) is done
    monkeypatch.setattr(freeconvert, "_job_status",
                        _Polls(_job("failed", _task("hardsub", "failed", message="boom"))))
    with pytest.raises(RuntimeError, match="boom"):
        await freeconvert._wait_for_job("k", "j")


async def test_failed_job_still_returns_the_quality_that_worked(
        monkeypatch, no_sleep):
    failed = _job("failed",
                  _task("import-video", "completed"),
                  _task("convert_360p", "completed"),
                  _task("export_360p", "completed", "https://fc/360.mp4"),
                  _task("convert_orig", "failed", message="Out of minutes"),
                  _task("export_orig", "created"))
    monkeypatch.setattr(freeconvert, "_job_status", _Polls(failed))
    job = await freeconvert._wait_for_job(
        "k", "j", ready=lambda j: freeconvert._multi_ready(j, ["360p", "orig"]))
    assert job is failed
    assert "Out of minutes" in freeconvert._task_problem(job, "orig")


async def test_failed_import_does_not_wait_for_the_full_timeout(
        monkeypatch, no_sleep):
    dead = _job("failed",
                _task("import-video", "failed", message="bad url"),
                _task("convert_360p", "waiting"),
                _task("export_360p", "created"))
    polls = _Polls(dead)
    monkeypatch.setattr(freeconvert, "_job_status", polls)
    await freeconvert._wait_for_job(
        "k", "j", ready=lambda j: freeconvert._multi_ready(j, ["360p"]))
    assert polls.calls == 1


async def test_multi_run_waits_for_both_qualities_and_never_duplicates_work(
        fc_net, monkeypatch, no_sleep):
    early = _job("completed",
                 _task("convert_360p", "completed"),
                 _task("export_360p", "completed", "https://fc/360p.mp4"),
                 _task("convert_720p", "processing"),
                 _task("export_720p", "created"))
    polls = _Polls(early, _fc_job_response())             # 720p lands on 2nd poll
    monkeypatch.setattr(freeconvert, "_job_status", polls)
    monkeypatch.setattr(freeconvert, "_wait_for_job", _real_wait)   # undo fixture fake
    out = await _call(fc_net)
    assert set(out) == {"360p", "720p"}
    assert polls.calls == 2
    assert fc_net.single_calls == []                      # no useless re-run


async def test_every_quality_failed_error_names_the_reason(
        fc_net, monkeypatch, no_sleep):
    dead = _job("failed", _task("import-video", "failed", message="bad url"),
                _task("convert_360p", "waiting"), _task("export_360p", "created"),
                _task("convert_720p", "waiting"), _task("export_720p", "created"))
    monkeypatch.setattr(freeconvert, "_job_status", _Polls(dead))
    monkeypatch.setattr(freeconvert, "_wait_for_job", _real_wait)
    fc_net.single_error = RuntimeError("still no")
    with pytest.raises(RuntimeError, match="bad url"):
        await _call(fc_net)
