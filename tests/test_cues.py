import asyncio

from autoeventplay.application.cues import CueExecutor
from autoeventplay.domain.model import CueAction, CuePhase, CueRule, MuteAction, Target, TargetKind


class FakeMixers:
    def __init__(self, can_query=True):
        self.can_query = can_query
        self.mute = {}
        self.level = {}
        self.calls = []

    async def get_mute(self, mid, t):
        return self.mute.get(t.key()) if self.can_query else None

    async def get_level(self, mid, t):
        return self.level.get(t.key()) if self.can_query else None

    async def set_mute(self, mid, t, muted):
        self.calls.append(("mute", t.number, muted))
        self.mute[t.key()] = muted

    async def set_level(self, mid, t, db):
        self.calls.append(("level", t.number, db))
        self.level[t.key()] = db

    def describe(self, mid, t):
        return f"ch{t.number}"


def rule(*actions, delay=0):
    return CueRule("r", "Test", actions=list(actions), pre_delay_ms=delay)


def ch(n):
    return Target(TargetKind.CHANNEL, n)


def test_unmute_level_and_restore_previous_values():
    mixers = FakeMixers()
    mixers.mute[ch(31).key()] = True
    mixers.level[ch(31).key()] = -20.0
    cues = CueExecutor(mixers)
    r = rule(CueAction("a", "m", ch(31), CuePhase.START, MuteAction.UNMUTE, 0.0, restore=True),
             CueAction("b", "m", ch(5), CuePhase.END, MuteAction.MUTE))

    async def run():
        s = await cues.start([r])
        assert mixers.mute[ch(31).key()] is False and mixers.level[ch(31).key()] == 0.0
        await cues.finish(s)

    asyncio.run(run())
    assert mixers.mute[ch(31).key()] is True
    assert mixers.level[ch(31).key()] == -20.0
    assert mixers.mute[ch(5).key()] is True
    assert cues.active_leases() == 0


def test_overlapping_playbacks_restore_only_after_last():
    mixers = FakeMixers()
    mixers.mute[ch(1).key()] = True
    cues = CueExecutor(mixers)
    r = rule(CueAction("a", "m", ch(1), CuePhase.START, MuteAction.UNMUTE, None, restore=True))

    async def run():
        s1 = await cues.start([r])
        s2 = await cues.start([r])
        await cues.finish(s1)
        assert mixers.mute[ch(1).key()] is False  # second playback still running
        await cues.finish(s2)
        assert mixers.mute[ch(1).key()] is True

    asyncio.run(run())


def test_fallback_without_query_support():
    mixers = FakeMixers(can_query=False)
    cues = CueExecutor(mixers)
    r = rule(CueAction("a", "m", ch(2), CuePhase.START, MuteAction.UNMUTE, -5.0, restore=True, restore_level_db=-30.0))

    async def run():
        await cues.finish(await cues.start([r]))

    asyncio.run(run())
    assert mixers.mute[ch(2).key()] is True       # inverse of "unmute"
    assert mixers.level[ch(2).key()] == -30.0     # configured fallback


def test_rule_matching():
    r = CueRule("r", "x", match_kind="speech")
    assert r.matches("tts", None, "") and r.matches("recording", None, "") and not r.matches("jingle", "f", "")
    r = CueRule("r", "x", match_kind="any", match_file_id="f1")
    assert r.matches("jingle", "f1", "") and not r.matches("jingle", "f2", "")
    r = CueRule("r", "x", match_title="sieger")
    assert r.matches("audio", None, "Siegerehrung") and not r.matches("audio", None, "Opener")
    assert not CueRule("r", "x", enabled=False).matches("tts", None, "")
