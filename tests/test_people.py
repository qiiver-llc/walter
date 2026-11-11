"""Who is there: matching, thresholds, storage, enrolment bookkeeping and how
the conductor / presence / ears use it. Synthetic embeddings only: no camera,
no microphone, no models, and nothing outside tmp_path."""
import asyncio
import os
import stat
import time

import numpy as np
import pytest

from walter.config import Config
from walter.conductor import Conductor
from walter.people import (TUNING, FaceEnrolment, FaceSample, Matcher, People, PeopleStore,
                           VoiceEnrolment, VoiceId, check_name, enrol_page, normalise, person_score,
                           slug, tune_threshold)

from test_conductor import FakeVoice

RNG = np.random.default_rng(7)


def person(dim=128, n=20, spread=0.35, rng=RNG):
    """n samples of one synthetic 'person': a fixed direction plus noise."""
    base = normalise(rng.normal(size=dim))[0]
    return base, normalise(base + spread * normalise(rng.normal(size=(n, dim))))


def near(base, spread=0.35, rng=RNG):
    return normalise(base + spread * normalise(rng.normal(size=base.shape[0])))[0]


@pytest.fixture
def people(tmp_path):
    cfg = Config()
    cfg.senses.recognise_faces = cfg.senses.recognise_voices = True
    return People(cfg, root=tmp_path / "people", models_dir=tmp_path / "models")


# ------------------------------------------------------------------ maths
def test_normalise_and_score():
    e = normalise([[3.0, 4.0], [0.0, 2.0]])
    assert np.allclose(np.linalg.norm(e, axis=1), 1)
    q = normalise([1.0, 0.0])[0]
    embs = normalise([[1, 0], [0.8, 0.6], [0, 1], [-1, 0]])
    assert person_score(q, embs, top_k=1) == pytest.approx(1.0)
    assert person_score(q, embs, top_k=2) == pytest.approx(0.9)


def test_threshold_stays_in_the_conservative_band():
    for kind in ("face", "voice"):
        t = TUNING[kind]
        _, tight = person(n=20, spread=0.05)
        thr, st = tune_threshold(tight, kind)
        assert thr == pytest.approx(t.cap)               # very consistent: as strict as allowed
        _, loose = person(n=20, spread=3.0)
        thr, st = tune_threshold(loose, kind)
        assert thr == pytest.approx(t.floor)             # very varied: never below the floor
        _, mid = person(n=20, spread=0.8)
        thr, st = tune_threshold(mid, kind)
        assert t.floor <= thr <= t.cap and st["samples"] == 20
        assert st["own_p5"] <= st["own_median"]


def test_threshold_ignores_same_group_samples():
    # pairs of near-identical windows (same sentence) must not inflate the threshold
    base, _ = person()
    embs, groups = [], []
    for g in range(5):
        s = near(base, spread=1.0)
        embs += [s, normalise(s + 0.01 * RNG.normal(size=128))[0]]
        groups += [g, g]
    with_groups, _ = tune_threshold(np.array(embs), "voice", groups)
    without, _ = tune_threshold(np.array(embs), "voice")
    assert with_groups <= without


def test_single_sample_uses_floor():
    thr, st = tune_threshold(normalise(RNG.normal(size=(1, 128))), "face")
    assert thr == TUNING["face"].floor


# --------------------------------------------------------------- matching
def test_match_genuine_and_reject_stranger():
    a_base, a = person()
    thr, _ = tune_threshold(a, "face")
    m = Matcher({"Alex": (a, thr)}, "face")
    hit = m.match(near(a_base))
    assert hit.name == "Alex" and hit.score >= thr
    stranger = normalise(RNG.normal(size=128))[0]
    miss = m.match(stranger)
    assert miss.name is None and miss.candidate == "Alex" and miss.score < thr


def test_two_lookalikes_are_ambiguous():
    base, a = person()
    b = normalise(a + 0.02 * RNG.normal(size=a.shape))   # practically the same person enrolled twice
    m = Matcher({"Alex": (a, 0.42), "Sam": (b, 0.42)}, "face")
    r = m.match(near(base, 0.1))
    assert r.name is None and r.score > 0.42             # close call: stays unknown


def test_two_people_told_apart():
    a_base, a = person()
    b_base, b = person()
    m = Matcher({"Alex": (a, 0.42), "Sam": (b, 0.42)}, "face")
    assert m.match(near(a_base)).name == "Alex"
    assert m.match(near(b_base)).name == "Sam"


def test_override_and_extra_raise_the_bar():
    base, a = person()
    q = near(base)
    score = Matcher({"Alex": (a, 0.0)}, "face", override=0.01).match(q).score
    assert Matcher({"Alex": (a, 0.4)}, "face", override=score + 0.01).match(q).name is None
    assert Matcher({"Alex": (a, score - 0.02)}, "face").match(q).name == "Alex"
    assert Matcher({"Alex": (a, score - 0.02)}, "face").match(q, extra=0.05).name is None


def test_empty_and_wrong_dimension():
    assert Matcher({}, "voice").match(RNG.normal(size=512)).name is None
    _, a = person(dim=128)
    assert Matcher({"Alex": (a, 0.4)}, "voice").match(RNG.normal(size=512)).name is None


# ---------------------------------------------------------------- storage
def test_names():
    assert check_name("  Alex  ") == "Alex"
    assert slug("Mary Jo O'Neil") == "mary-jo-o-neil"
    for bad in ("", "../etc", "a/b", "1abc", "x" * 41, "Al;ex"):
        with pytest.raises(ValueError):
            check_name(bad)


def test_store_roundtrip_and_privacy(tmp_path):
    st = PeopleStore(tmp_path / "people")
    assert st.people() == [] and st.load("face") == {}
    _, a = person()
    st.save("Alex", "face", a, {"threshold": 0.5})
    _, v = person(dim=512, n=12)
    st.save("Alex", "voice", v, {"threshold": 0.45})
    d = tmp_path / "people" / "alex"
    assert sorted(p.name for p in d.iterdir()) == ["face.npy", "meta.json", "voice.npy"]  # numbers only
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / "face.npy").st_mode) == 0o600
    np.load(d / "face.npy", allow_pickle=False)                 # plain arrays, no pickles
    rows = st.people()
    assert rows[0]["name"] == "Alex" and rows[0]["face"]["samples"] == 20
    assert rows[0]["voice"]["dim"] == 512
    faces = st.load("face")
    assert faces["Alex"][1] == 0.5 and faces["Alex"][0].shape == (20, 128)
    assert st.forget("Alex", "voice") == ["voice"]
    assert st.people()[0]["voice"] is None and d.exists()
    assert st.forget("Alex") == ["face"]
    assert not d.exists() and st.people() == []
    assert st.forget("Nobody") == []


def test_people_save_tunes_and_reloads(people):
    base, a = person(spread=0.8)
    meta = people.save("Alex", "face", a)
    thr = meta["face"]["threshold"]
    assert TUNING["face"].floor <= thr <= TUNING["face"].cap
    assert len(people.face) == 1 and len(people.voice) == 0
    assert people.face.match(near(base, 0.3)).name == "Alex"
    assert people.list()[0]["face"]["samples"] == 20
    assert people.forget("alex", "face") == ["face"]
    assert len(people.face) == 0


def test_config_threshold_override(tmp_path):
    cfg = Config()
    cfg.senses.face_threshold = 0.99
    p = People(cfg, root=tmp_path / "people", models_dir=tmp_path)
    base, a = person()
    p.save("Alex", "face", a)
    assert p.face.match(near(base)).name is None


# ------------------------------------------------------------- enrolment
def face(emb, size=100):
    return FaceSample(np.asarray(emb, np.float32), 0.5, size, 0.95, 100.0)


def test_face_enrolment_collects_good_varied_samples():
    base, _ = person()
    s = FaceEnrolment(target=20, per_phase=8)
    t = 0.0
    s.feed([], t)
    assert s.n == 0 and s.note == "no clear face"
    s.feed([face(near(base)), face(near(base))], t)
    assert s.n == 0 and "more than one" in s.note
    for _ in range(12):                      # one angle: capped at 8
        t += 0.5
        s.feed([face(near(base))], t)
    assert s.n == 8 and s.note == "got this angle"
    s.set_phase(1, cap=8)
    t += 0.5
    s.feed([face(near(base))], t)
    assert s.n == 9                          # a new angle: collecting again
    s.feed([face(near(base))], t + 0.1)      # too soon after the last one
    assert s.n == 9
    t += 0.5
    s.feed([face(s.embs[-1])], t)            # identical: adds nothing
    assert s.n == 9
    s.feed([face(normalise(RNG.normal(size=128))[0])], t + 0.5)   # someone else walked past
    assert s.n == 9 and "someone else" in s.note
    s.set_phase(3, cap=None)
    for _ in range(20):
        t += 0.5
        s.feed([face(near(base))], t)
    assert s.n == 29 and s.result().shape == (29, 128)


def test_voice_enrolment_groups_and_page():
    s = VoiceEnrolment("Alex", sentences=2)
    s.feed([], "nothing")
    assert s.heard == 0
    s.feed([np.ones(4), np.ones(4), np.ones(4)], "one")
    s.feed([np.ones(4)], "two")
    assert s.done and s.groups == [0, 0, 0, 1] and len(s.embs) == 4
    page = enrol_page("Alex", 1)
    assert "Alex" in page and "(heard)" in page and "1 of 5 heard" in page


class FakeVoiceModel:
    def __init__(self, emb):
        self.emb = emb

    def embed(self, audio):
        return self.emb if len(audio) >= 16000 else None

    def windows(self, audio):
        return [self.emb, self.emb]


def test_voice_id_matches_and_enrols(people):
    base, v = person(dim=512, n=12, spread=0.8)
    people.save("Alex", "voice", v)
    vid = VoiceId(people, FakeVoiceModel(near(base, 0.3)))
    pcm = np.zeros(16000 * 3, np.int16).tobytes()
    res = vid.analyse(pcm)
    assert res["match"]["name"] == "Alex" and res["enrol"] is None and res["dur"] == 3.0
    vid.enrol = VoiceEnrolment("Sam")
    assert len(vid.analyse(pcm)["enrol"]) == 2
    assert vid.analyse(b"\0\0" * 8000)["match"] is None         # 0.5 s: too short to judge


# ---------------------------------------------------- conductor / presence
def make_conductor(people=None):
    sent = []
    cfg = people.cfg if people else Config()
    c = Conductor(cfg, brain=None, send=sent.append)
    c.voice = FakeVoice()
    c.in_quiet_hours = lambda: False
    c.people = people
    return c, sent


def test_heard_utterance_gets_speaker_label(people):
    async def go():
        _, v = person(dim=512, n=6)
        people.save("Alex", "voice", v)
        c, _ = make_conductor(people)
        c.on_hear_speaker({"name": "Alex", "score": 0.7, "candidate": "Alex", "threshold": 0.5})
        c.on_heard("Hey Walter, what's on today?")
        src, text, meta = c.inbox.get_nowait()
        frame = c._frame(src, text, meta)
        assert "speaker: Alex (voice match)]" in frame and frame.endswith("what's on today?")
        c.on_hear_speaker({"name": None, "score": 0.2, "candidate": "Alex", "threshold": 0.5})
        c.on_heard("Walter, and tomorrow?")
        assert "speaker: unknown]" in c._frame(*c.inbox.get_nowait())
        c.on_heard("Walter, one more")                      # no voice result (too short)
        assert "speaker: unknown]" in c._frame(*c.inbox.get_nowait())
    asyncio.run(go())


def test_no_label_without_enrolled_voices():
    async def go():
        c, _ = make_conductor()
        c.on_heard("Hey Walter, what's the weather like?")
        assert c.inbox.get_nowait() == ("voice", "Hey Walter, what's the weather like?")
    asyncio.run(go())


def test_voice_enrolment_sentences_never_reach_the_brain(people):
    async def go():
        c, sent = make_conductor(people)
        c.enrolling = "voice"
        c.followup_until = time.time() + 30
        c.on_heard("Please remind me to water the tomatoes on Thursday morning.")
        assert c.inbox.empty()
        assert {"t": "caption", "who": "you",
                "text": "Please remind me to water the tomatoes on Thursday morning."} in sent
    asyncio.run(go())


def test_arrival_names_who_walked_in():
    async def go():
        c, _ = make_conductor()
        c.on_presence_arrival({"e": "arrival", "away_s": 1500, "greet": True, "who": ["Alex"]})
        src, text, meta = c.inbox.get_nowait()
        assert text.startswith("Alex just walked in after 25 minutes")
        assert "recognised by face" in c._frame(src, text, meta)
        c2, _ = make_conductor()
        c2.on_presence_arrival({"e": "arrival", "away_s": 1500, "greet": True, "who": ["someone"]})
        src, text = c2.inbox.get_nowait()
        assert text.startswith("Someone just walked in") and "can't see who" in c2._frame(src, text)
    asyncio.run(go())


def test_presence_who_and_delayed_arrival(people):
    from walter.presence import Presence

    async def go():
        base, a = person()
        people.save("Alex", "face", a)
        c, sent = make_conductor(people)
        p = Presence(people.cfg, c, sent.append)
        p.people = people
        p.loop = asyncio.get_running_loop()
        assert p.recognise and p.who_now() == []
        p.tracker.present = True
        assert p.who_now() == ["someone"]
        arrivals = []
        c.on_presence_arrival = arrivals.append
        p._event({"e": "arrival", "away_s": 900, "greet": True, "since_start": False, "x": 0.5})
        assert arrivals == [] and p._pending_arrival is not None     # waiting for a face
        p._saw_faces(["Alex", None], time.time())
        assert arrivals and arrivals[0]["who"] == ["Alex", "someone"]
        assert p._pending_arrival is None
        p._flush_arrival()                                             # the timer firing later: no-op
        assert len(arrivals) == 1
        st = p.status()
        assert st["faces_enrolled"] == 1 and st["who"] == ["Alex", "someone"]
        p.seen_people["Alex"] = time.time() - 100
        p.unknown_face_t = -1e18
        assert p.who_now() == ["someone"]
        assert "in view: Alex" not in (c.speaker_label(None) or "")
        p.seen_people["Alex"] = time.time()
        c.presence = p
        assert c.speaker_label(None) == "in view: Alex (face)"
    asyncio.run(go())


# ------------------------------------------------------------------- ears
def test_ears_emit_speaker_before_text(people):
    from walter.ears import Ears

    class SyncLoop:
        def call_soon_threadsafe(self, fn, *a):
            fn(*a)

    base, v = person(dim=512, n=6, spread=0.8)
    people.save("Alex", "voice", v)
    order = []
    e = Ears(people.cfg, on_text=lambda t: order.append(("text", t)), on_hear=lambda v: None,
             on_start=lambda: None, on_speaker=lambda m: order.append(("speaker", m["name"])))
    e.loop = SyncLoop()
    e.voice_id = VoiceId(people, FakeVoiceModel(near(base, 0.3)))
    e._recognise = lambda pcm, final=False, **kw: ("Hey Walter, it's me", [])
    pcm = np.zeros(16000 * 2, np.int16).tobytes()
    assert e._speaker_job([pcm], speech_frames=10) is None      # 0.3 s of speech: not worth it
    job = e._speaker_job([pcm], speech_frames=60)
    assert e._transcribe(pcm, spk=job)
    assert order == [("speaker", "Alex"), ("text", "Hey Walter, it's me")]
    # enrolment: the sentence's windows go to the session
    e.voice_id.enrol = VoiceEnrolment("Alex")
    assert e._transcribe(pcm, spk=e._speaker_job([pcm], speech_frames=60))
    assert e.voice_id.enrol.heard == 1 and len(e.voice_id.enrol.embs) == 2
    e._spk_pool.shutdown()
