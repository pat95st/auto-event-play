"""End-to-end: pair with a fake eventrentmanager, cache files, play a queued
jingle, drive a fake X32 via a cue, report status back."""

import asyncio
from pathlib import Path

import pytest

from autoeventplay.application import commands as c
from autoeventplay.application import queries as q
from autoeventplay.application.bus import DomainError
from autoeventplay.config import AppConfig
from autoeventplay.container import Services
from tests.fakes import FakeErm, FakeOscConsole


@pytest.fixture()
def env(tmp_path: Path):
    erm = FakeErm().start()
    console = FakeOscConsole().start()
    services = Services(AppConfig(data_dir=tmp_path, audio_backend="null", port=0))
    yield erm, console, services
    erm.stop()
    console.stop()


async def wait_for(pred, timeout=8.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def test_pair_play_and_cue(env):
    erm, console, s = env

    async def run():
        await s.start()
        try:
            with pytest.raises(DomainError):
                await s.commands.dispatch(c.PairWithErm(erm.url, "999999", "Pi FOH"))
            result = await s.commands.dispatch(c.PairWithErm(erm.url, "123 456", "Pi FOH"))
            assert result["rental"]["name"] == "Stadtfest"
            assert s.settings.get("erm.token") == erm.token
            assert erm.pair_calls[-1]["body"]["name"] == "Pi FOH"

            assert await wait_for(lambda: s.file_repo.get("f1") is not None), "file not cached"
            assert Path(s.file_repo.get("f1")["path"]).exists()

            mixer_id = await s.commands.dispatch(c.SaveMixer(None, "FOH", "behringer_x32", "127.0.0.1", console.port, True, {}))
            console.values["/ch/31/mix/on"] = 0          # muted before
            console.values["/ch/31/mix/fader"] = 0.25    # -30 dB before
            await s.commands.dispatch(c.SaveCueRule({
                "name": "Jingles über FOH", "match_kind": "media", "pre_delay_ms": 0,
                "actions": [
                    {"phase": "start", "mixer_id": mixer_id, "target": {"kind": "channel", "number": 31}, "mute": "unmute", "level_db": 0, "restore": True},
                    {"phase": "end", "mixer_id": mixer_id, "target": {"kind": "bus", "number": 2}, "mute": "mute"},
                ],
            }))

            erm.add_job("job1", "jingle")
            s.sync.wake()
            assert await wait_for(lambda: ("job1", "playing", None) in erm.status_reports), erm.status_reports
            assert await wait_for(lambda: console.values.get("/ch/31/mix/on") == 1)
            assert console.values["/ch/31/mix/fader"] == pytest.approx(0.75)
            assert await wait_for(lambda: ("job1", "done", None) in erm.status_reports, 10), erm.status_reports
            assert await wait_for(lambda: console.values.get("/ch/31/mix/on") == 0)
            assert console.values["/ch/31/mix/fader"] == pytest.approx(0.25)
            assert console.values.get("/bus/02/mix/on") == 0

            # already processed jobs are not played twice
            erm.queue[-1].pop("_status")
            await asyncio.sleep(1.5)
            assert [r for r in erm.status_reports if r[0] == "job1" and r[1] == "playing"] == [("job1", "playing", None)]

            dash = await s.queries.dispatch(q.GetDashboard())
            assert dash["erm"]["status"]["online"] is True
            assert dash["files"][0]["title"] == "Opener"
        finally:
            await s.shutdown()

    asyncio.run(run())


def test_failed_file_is_reported(env):
    erm, console, s = env

    async def run():
        await s.start()
        try:
            await s.commands.dispatch(c.PairWithErm(erm.url, "123456", "Pi"))
            erm.files["f2"] = {**erm.files["f1"], "id": "f2", "version": "f2.wav", "url": "/api/player/v1/files/f2", "title": "Kaputt"}
            erm.file_data["f2"] = b"not audio at all"
            erm.add_job("job2", "audio", file_id="f2", label="Kaputt")
            s.sync.wake()
            assert await wait_for(lambda: any(r[0] == "job2" and r[1] == "failed" for r in erm.status_reports), 10), erm.status_reports
        finally:
            await s.shutdown()

    asyncio.run(run())


def test_stop_request_cancels_running_playback(env):
    erm, console, s = env
    from tests.fakes import make_wav

    async def run():
        await s.start()
        try:
            await s.commands.dispatch(c.PairWithErm(erm.url, "123456", "Pi"))
            erm.file_data["f1"] = make_wav(seconds=20)
            erm.add_job("long", "jingle")
            s.sync.wake()
            assert await wait_for(lambda: ("long", "playing", None) in erm.status_reports)
            await asyncio.sleep(0.5)
            erm.add_job("stop1", "stop", file_id=None)
            s.sync.wake()
            assert await wait_for(lambda: ("long", "cancelled", None) in erm.status_reports, 6), erm.status_reports
            assert await wait_for(lambda: ("stop1", "done", None) in erm.status_reports, 6)
        finally:
            await s.shutdown()

    asyncio.run(run())
