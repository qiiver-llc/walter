"""Media handoff (walter/facehandoff.py): what plays on the wall moves to another face, and is
stopped here only once the face took it. mpv, podcasts and radio are all faked."""
from walter import facehandoff as H


class Pod:
    def __init__(self, now=None, running=True, pos=125.0):
        self.st = {"now": now}
        self.running, self.pos, self.stopped = running, pos, False

    def load(self):
        return self.st

    def player_running(self):
        return self.running

    def get(self, prop):
        return self.pos if prop == "time-pos" else None

    def stop(self, st, quiet=False):
        self.stopped = True


class Radio(Pod):
    pass


EP = {"url": "https://feeds.example/ep.mp3", "show": "A Show", "title": "Ep 1"}


def test_media_now_prefers_the_podcast_then_radio():
    pod, radio = Pod(EP), Radio({"url": "https://radio.example/stream", "name": "KXYZ"})
    now = H.media_now(pod, radio)
    assert now["source"] == "podcast" and now["start"] == 123.0 and now["title"] == "A Show: Ep 1"
    pod.running = False
    now = H.media_now(pod, radio)
    assert now["source"] == "radio" and now["title"] == "KXYZ" and now["start"] == 0
    radio.running = False
    assert H.media_now(pod, radio) is None


def test_handoff_starts_there_then_stops_here():
    pod, radio = Pod(EP), Radio(None)
    calls = []
    ok, msg = H.handoff_media("kitchen", lambda p, b: calls.append((p, b)) or {"face": "Kitchen"},
                              podcast_mod=pod, radio_mod=radio)
    assert ok and "Kitchen" in msg and pod.stopped
    path, body = calls[0]
    assert path == "/api/faces/send" and body["kind"] == "media" and body["url"] == EP["url"]
    assert body["start"] == 123.0 and body["face"] == "kitchen"


def test_handoff_failure_keeps_playing_here():
    pod = Pod(EP)

    def refuse(p, b):
        raise RuntimeError("Kitchen has no speaker agent online")
    ok, msg = H.handoff_media("kitchen", refuse, podcast_mod=pod, radio_mod=Radio(None))
    assert not ok and "speaker" in msg and not pod.stopped
    ok, msg = H.handoff_media("kitchen", refuse, podcast_mod=Pod(None), radio_mod=Radio(None))
    assert not ok and "nothing is playing" in msg
    bad = dict(EP, url="file:///etc/passwd")
    ok, msg = H.handoff_media("kitchen", refuse, podcast_mod=Pod(bad), radio_mod=Radio(None))
    assert not ok and not "speaker" in msg
