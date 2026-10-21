"""FreeConvert pipeline: subtitle choice, sizing, cancellation and the
run_fc_job branches (done / needs a subtitle), with every network or ffmpeg
call replaced by a fake."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.config import Config
from bot.database.connection import Database
from bot.jobs import fc_pipeline
from bot.jobs.models import InputType, Job, JobKind, new_job_id
from bot.jobs.repository import JobRepository
from bot.jobs.states import JobStatus
from bot.runtime import Runtime
from bot.services.ffprobe import MediaInfo, SubtitleStream
from bot.services.thumbnail import ThumbSet
from bot.utils.subprocesses import CancelToken
from tests.conftest import make_media
from tests.test_fc_flow import _ASS


def _sub(idx, lang, codec="ass", default=False, forced=False):
    return SubtitleStream(index=idx, type_index=idx, codec=codec, language=lang,
                          title="", default=default, forced=forced)


def _media(subs, width=1920, height=1080) -> MediaInfo:
    m = make_media(width=width, height=height)
    m.subtitles = subs
    return m


# ── French track choice ─────────────────────────────────────────────────

def test_prefers_full_french_over_forced():
    m = _media([_sub(0, "eng", default=True), _sub(1, "fre", forced=True),
                _sub(2, "fre")])
    assert fc_pipeline.pick_french_stream(m).type_index == 2


def test_french_iso_variants_match():
    assert fc_pipeline.pick_french_stream(_media([_sub(0, "fra")])).type_index == 0
    assert fc_pipeline.pick_french_stream(_media([_sub(0, "fre")])).type_index == 0


def test_never_falls_back_to_another_language():
    assert fc_pipeline.pick_french_stream(
        _media([_sub(0, "eng", default=True), _sub(1, "spa")])) is None


def test_bitmap_french_track_is_skipped():
    m = _media([_sub(0, "fre", codec="hdmv_pgs_subtitle")])
    assert fc_pipeline.pick_french_stream(m) is None


# ── output size ─────────────────────────────────────────────────────────

def test_original_means_no_resize():
    assert fc_pipeline.target_size(0, 1920, 1080) is None


def test_standard_sizes_without_probe():
    assert fc_pipeline.target_size(360) == (640, 360)
    assert fc_pipeline.target_size(720) == (1280, 720)
    assert fc_pipeline.target_size(1080) == (1920, 1080)


def test_source_ratio_is_kept_with_even_width():
    assert fc_pipeline.target_size(720, 1920, 1080) == (1280, 720)
    assert fc_pipeline.target_size(720, 640, 480) == (960, 720)     # 4:3
    w, h = fc_pipeline.target_size(480, 1920, 804)                  # scope
    assert h == 480 and w % 2 == 0 and abs(w / h - 1920 / 804) < 0.02


# ── cancellation ────────────────────────────────────────────────────────

async def test_cancel_stops_the_cloud_wait():
    token = CancelToken()
    state = {"cancelled": False}

    async def slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    task = asyncio.create_task(fc_pipeline._cancellable(slow(), token))
    await asyncio.sleep(0.1)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert state["cancelled"]


async def test_result_is_returned_when_not_cancelled():
    async def quick():
        return "done"
    assert await fc_pipeline._cancellable(quick(), CancelToken()) == "done"


# ── run_fc_job ──────────────────────────────────────────────────────────

class StubReporter:
    def __init__(self):
        self.texts: list[str] = []

    async def update(self, text, reply_markup=None, force=False):
        self.texts.append(text)


class _Sem:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
async def env(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.connect()
    repo = JobRepository(db)
    cfg = Config(api_id=1, api_hash="h", bot_token="t", owner_id=1,
                 work_dir=tmp_path / "work", fc_api_keys=("k1", "k2"),
                 min_free_disk_gb=0.0)
    rt = Runtime(config=cfg, db=db, repo=repo,
                 manager=SimpleNamespace(upload_sem=_Sem()), client=object())
    calls: dict = {"fc": [], "uploads": []}

    async def fake_fc(api_keys, video_url, source_name, subtitle_path, dest_dir, **kw):
        calls["fc"].append({"keys": api_keys, "url": video_url,
                            "sub": subtitle_path, **kw})
        out = Path(dest_dir) / "Show EP01 VOSTFR.mp4"
        out.write_bytes(b"\0" * 2048)
        return str(out)

    async def fake_upload(client, chat_id, path, **kw):
        calls["uploads"].append((str(path), kw))

    async def fake_thumbs(*a, **k):
        return ThumbSet()

    monkeypatch.setattr(fc_pipeline.freeconvert, "hardsub_remote_url", fake_fc)
    monkeypatch.setattr(fc_pipeline, "upload_file", fake_upload)
    monkeypatch.setattr(fc_pipeline, "make_thumbnails", fake_thumbs)
    yield SimpleNamespace(rt=rt, calls=calls, tmp=tmp_path, monkeypatch=monkeypatch)
    await db.close()


async def _queued_job(env, *, source, sub_text=_ASS, **opts) -> Job:
    job = Job(
        id=new_job_id(), user_id=7, chat_id=7, kind=JobKind.FC_HARDSUB,
        status=JobStatus.QUEUED, input_type=InputType.URL,
        input_ref="https://cdn.example/Show.S01E01.mkv",
        input_name="Show.S01E01.mkv",
        options={"engine": "fc", "fc_res": 720, "fc_style": "b",
                 "subtitle_mode": "hard", "subtitle_source": source, **opts},
    )
    work = env.tmp / "work" / "job"
    (work / "subtitles").mkdir(parents=True)
    job.work_dir = str(work)
    if sub_text is not None:
        sub = work / "subtitles" / "upload.ass"
        sub.write_text(sub_text, encoding="utf-8")
        job.options["subtitle_file"] = str(sub)
    await env.rt.repo.insert(job)
    return job


async def _run(env, job):
    job = await env.rt.repo.get(job.id)
    return await fc_pipeline.run_fc_job(
        env.rt, job, CancelToken(), StubReporter(), 0.0, {}, "A")


async def test_uploaded_subtitle_runs_to_completion(env):
    job = await _queued_job(env, source="upload")
    await _run(env, job)

    (fc,) = env.calls["fc"]
    assert fc["url"] == "https://cdn.example/Show.S01E01.mkv"   # no local download
    assert fc["keys"] == "k1,k2"
    assert fc["resize"] == (1280, 720) and fc["style_key"] == "b"
    assert fc["codec"] == "h264"
    assert len(env.calls["uploads"]) == 1
    assert (await env.rt.repo.get(job.id)).status == JobStatus.COMPLETED


async def test_original_quality_sends_no_resize(env):
    job = await _queued_job(env, source="upload", fc_res=0, fc_style="orig")
    await _run(env, job)
    (fc,) = env.calls["fc"]
    assert fc["resize"] is None and fc["style_key"] == "orig"


async def test_french_track_is_extracted_automatically(env):
    job = await _queued_job(env, source="embedded_auto", sub_text=None)
    extracted = {}

    async def fake_probe(path, **kw):
        return _media([_sub(0, "eng", default=True), _sub(1, "fre")],
                      width=640, height=480)

    async def fake_extract(url, stream, out_dir, **kw):
        extracted.update(url=url, stream=stream.type_index, timeout=kw["timeout"])
        p = Path(out_dir) / "embedded.ass"
        p.write_text(_ASS, encoding="utf-8")
        return str(p)

    env.monkeypatch.setattr(fc_pipeline, "probe_media", fake_probe)
    env.monkeypatch.setattr(fc_pipeline, "extract_text_subtitle", fake_extract)
    await _run(env, job)

    assert extracted["url"] == "https://cdn.example/Show.S01E01.mkv"
    assert extracted["stream"] == 1                      # the French track
    assert extracted["timeout"] >= 1800                  # remote files are slow
    (fc,) = env.calls["fc"]
    assert fc["resize"] == (960, 720)                    # 4:3 source kept
    assert (await env.rt.repo.get(job.id)).status == JobStatus.COMPLETED


async def test_no_french_track_pauses_and_asks_for_a_subtitle(env):
    job = await _queued_job(env, source="embedded_auto", sub_text=None)

    async def fake_probe(path, **kw):
        return _media([_sub(0, "eng", default=True)])

    env.monkeypatch.setattr(fc_pipeline, "probe_media", fake_probe)
    await _run(env, job)

    after = await env.rt.repo.get(job.id)
    assert after.status == JobStatus.WAITING_FOR_INPUT
    assert after.options["await"] == "subtitle"
    assert not env.calls["fc"]                           # nothing sent to the cloud
    # the existing upload handler can now find this job
    waiting = await env.rt.repo.awaiting_job_for_user(7)
    assert waiting is not None and waiting.id == job.id


async def test_missing_uploaded_file_asks_again_instead_of_failing(env):
    job = await _queued_job(env, source="upload", sub_text=None)
    await _run(env, job)
    after = await env.rt.repo.get(job.id)
    assert after.status == JobStatus.WAITING_FOR_INPUT
    assert not env.calls["fc"]


async def test_oversized_result_is_refused(env):
    env.rt.config = Config(api_id=1, api_hash="h", bot_token="t", owner_id=1,
                           work_dir=env.tmp / "work", fc_api_keys=("k1",),
                           min_free_disk_gb=0.0, max_output_size=1024)
    job = await _queued_job(env, source="upload")
    from bot.utils.validation import ValidationError
    with pytest.raises(ValidationError, match="exceeds"):
        await _run(env, job)
    assert not env.calls["uploads"]


async def test_not_configured_is_a_clear_error(env):
    env.rt.config = Config(api_id=1, api_hash="h", bot_token="t", owner_id=1,
                           work_dir=env.tmp / "work")
    job = await _queued_job(env, source="upload")
    from bot.utils.validation import ValidationError
    with pytest.raises(ValidationError, match="FC_API_KEYS"):
        await _run(env, job)
