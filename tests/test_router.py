import asyncio
import sys
from pathlib import Path

from walter.brain import Brain
from walter.config import Config
from walter.conductor import Conductor, DEEP_NOTE, NO_ESCALATE_NOTE, REASK_NOTE
from walter.router import EscalationGate, Router, classify


# ------------------------------------------------------------ heuristic
def kind(t):
    return classify(t)[0]


def test_small_talk_and_quick_questions_stay_fast():
    for t in ["hey walter", "how are you doing", "what time is it", "thanks!",
              "what's the weather like tomorrow", "set a timer for ten minutes",
              "who won the game last night", "tell me a joke"]:
        assert kind(t) == "neutral", t


def test_hard_turns_go_deep():
    for t in ["can you help me plan the trip to the lake next month",
              "compare these two laptops for me",
              "research the best lawn mowers under five hundred dollars",
              "why does my python script throw an exception when it starts",
              "debug the face code, the eyes stopped blinking",
              "write me a proposal and then summarize the costs",
              "what are the pros and cons of solar panels",
              "walk me through setting up the router"]:
        assert kind(t) == "deep", (t, classify(t))


def test_weak_signal_alone_is_not_enough():
    assert kind("explain that again") == "neutral"
    assert kind("can you fix the reminder") == "neutral"


def test_explicit_overrides():
    assert classify("think hard about this: what's two plus two")[:2] == ("deep", 99)
    assert kind("use the big brain for this one") == "deep"
    assert kind("quick answer: compare a mile and a kilometre") == "fast"
    assert kind("just tell me the debug flag") == "fast"


def test_long_multipart_request_goes_deep():
    t = ("so I was wondering whether we should get the kids a new trampoline this year "
         "or wait until spring when the prices drop, and also what size fits the back yard?")
    assert kind(t) == "deep"


# ------------------------------------------------------------ hysteresis
def test_followups_stick_then_drop_back():
    r = Router()
    assert r.decide("help me plan the garden layout", now=0).deep
    d1 = r.decide("okay", now=10)
    d2 = r.decide("sounds good", now=20)
    assert d1.deep and d2.deep and "follow-up" in d1.reason
    d3 = r.decide("cool", now=30)
    assert not d3.deep and "back to fast" in d3.reason


def test_deep_streak_ends_after_idle():
    r = Router()
    r.decide("research heat pumps", now=0)
    d = r.decide("and the noise?", now=301)
    assert not d.deep and "quiet" in d.reason


def test_quick_answer_ends_streak():
    r = Router()
    r.decide("research heat pumps", now=0)
    assert not r.decide("quick answer, which is cheaper", now=5).deep
    assert not r.decide("ok", now=6).deep


def test_background_turns_are_fast_and_keep_streak():
    r = Router()
    r.decide("help me debug the build", now=0)
    assert not r.decide("[heartbeat]", source="heartbeat", now=5).deep
    assert r.decide("so what now", now=10).deep


def test_modes_and_persistence(tmp_path):
    f = tmp_path / "mode.json"
    r = Router(state_file=f)
    r.set_mode("deep")
    assert r.decide("hi").deep and "forced" in r.last.reason
    assert Router(state_file=f).mode == "deep"
    r.set_mode("fast")
    assert not r.decide("research and compare everything").deep
    assert not Router(auto_switch=False).decide("debug my code").deep


def test_escalation_starts_a_deep_streak():
    r = Router()
    r.decide("hello", now=0)
    assert r.escalated(now=1).deep
    assert r.decide("ok", now=2).deep


# ------------------------------------------------------------ the gate
def test_gate_catches_tag_split_across_chunks():
    g = EscalationGate()
    out = [g.feed(c) for c in ["\n", " [thi", "nk:h", "ard] let me think"]]
    assert g.escalate and "".join(out) == ""


def test_gate_passes_normal_replies():
    g = EscalationGate()
    assert g.feed("[th") == ""
    assert g.feed("inking] One sec.") == "[thinking] One sec."
    assert g.feed(" more") == " more" and not g.escalate


def test_gate_disabled_passes_everything():
    g = EscalationGate(enabled=False)
    assert g.feed("[think:hard] x") == "[think:hard] x"


# ------------------------------------------------------------ conductor re-ask
class FakeVoice:
    generation = 0

    def __init__(self):
        self.said, self.cues = [], []

    def say(self, t):
        self.said.append(t)

    def put(self, k, v=None, sound=True):
        self.cues.append((k, v))

    def stop(self):
        self.generation += 1

    async def wait_idle(self):
        pass


class FakeBrain:
    """Replies per model; the fast model escalates when it sees 'ESCALATE'."""

    def __init__(self):
        self.calls, self.interrupts = [], 0

    async def interrupt(self):
        self.interrupts += 1

    async def ask(self, text, model=None):
        self.calls.append((model, text))
        if model == "sonnet" and "ESCALATE" in text:
            chunks = ["[think", ":hard]", " I'd better hand this over. ", "Secret fast words."]
        else:
            chunks = [f"[happy] Answer from {model}."]
        yield ("text_start", "")
        for c in chunks:
            yield ("text", c)
        yield ("done", "".join(chunks))


def make(tmp_path):
    cfg = Config()
    cfg.brain.fast_model, cfg.brain.deep_model = "sonnet", "opus"
    c = Conductor(cfg, brain=FakeBrain(), send=lambda m: None)
    c.voice = FakeVoice()
    return c


def test_easy_turn_uses_fast_model(tmp_path):
    c = make(tmp_path)
    asyncio.run(c._respond("voice", "how are you"))
    assert [m for m, _ in c.brain.calls] == ["sonnet"]
    assert c.voice.said == ["Answer from sonnet."]


def test_hard_turn_goes_straight_to_deep(tmp_path):
    c = make(tmp_path)
    asyncio.run(c._respond("voice", "help me plan and compare two vacations"))
    assert [m for m, _ in c.brain.calls] == ["opus"]
    assert c.brain.calls[0][1].startswith(DEEP_NOTE)   # so it never escalates itself


def test_escalation_discards_fast_reply_and_reasks_deep(tmp_path):
    c = make(tmp_path)
    asyncio.run(c._respond("voice", "ESCALATE please"))
    models = [m for m, _ in c.brain.calls]
    assert models == ["sonnet", "opus"]
    assert c.brain.interrupts == 1
    assert c.voice.said == ["Answer from opus."]            # nothing of the fast reply
    assert c.brain.calls[1][1].startswith(REASK_NOTE)
    assert "ESCALATE please" in c.brain.calls[1][1]
    assert c.router.last.deep and "escalated" in c.router.last.reason


def test_no_escalation_when_forced_fast(tmp_path):
    c = make(tmp_path)
    c.router.set_mode("fast")
    asyncio.run(c._respond("voice", "ESCALATE please"))
    assert [m for m, _ in c.brain.calls] == ["sonnet"]   # tag just gets dropped by the tag parser
    assert "Secret fast words." in " ".join(c.voice.said)
    assert c.brain.calls[0][1].startswith(NO_ESCALATE_NOTE)


# ------------------------------------------------------------ brain set_model
def test_brain_switches_model_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(Brain, "_load_session", lambda self: None)   # don't read ~/.walter
    fake = Path(__file__).with_name("fake_claude.py")
    exe = tmp_path / "claude"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
    exe.chmod(0o755)
    cfg = Config()
    cfg.brain.claude_bin = str(exe)
    cfg.brain.home = str(tmp_path / "home")
    cfg.brain.fast_model, cfg.brain.deep_model = "sonnet", "opus"

    async def go():
        b = Brain(cfg)
        b.state_file = tmp_path / "brain.json"

        async def reply(model):
            return "".join(v for k, v in [x async for x in b.ask("which model are you", model=model)]
                           if k == "text")
        try:
            assert "sonnet" in await reply(None)
            pid = b.proc.pid
            assert "opus" in await reply("opus")
            assert b.proc.pid == pid and b.model == "opus"     # same process, no restart
            assert b.switches == 1 and b.last_switch_s < 2
            assert "sonnet" in await reply("sonnet")
        finally:
            await b.close()
    asyncio.run(go())
