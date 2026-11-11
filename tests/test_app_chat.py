"""Companion-app turns reach the wall's conversation chat thread (face/chat.js) as
{t:"chat", who, text, from:"phone"}: the person's words on submit, and my reply on
finish for app-only turns ("both" turns are spoken on the wall, so captions cover them)."""
from types import SimpleNamespace

from walter.app_server import AppHub, AppReply


class Hub:
    def __init__(self):
        self.face, self.dev, self.current = [], [], None

    def to_device(self, device_id, msg):
        self.dev.append(msg)

    def to_face(self, msg):
        self.face.append(msg)


def test_app_only_reply_goes_to_the_wall_chat():
    hub = Hub()
    r = AppReply(hub, {"route": "app", "device_id": "d1", "app_turn": 1})
    r.raw("[happy] Added **milk** to the list.")
    r.finish()
    assert hub.face == [{"t": "chat", "who": "walter", "text": "Added **milk** to the list.", "from": "phone"}]


def test_both_route_silent_and_cancelled_replies_stay_off_the_chat():
    for meta, silent, cancel in (({"route": "both"}, False, False), ({"route": "app"}, True, False),
                                 ({"route": "app"}, False, True)):
        hub = Hub()
        r = AppReply(hub, dict(meta, device_id="d1", app_turn=1))
        r.raw("Hello.")
        r.cancelled = cancel
        r.finish(silent=silent)
        assert hub.face == []


def test_submit_puts_the_words_in_the_chat_and_to_face_never_raises():
    sent, submitted = [], []
    conductor = SimpleNamespace(send=sent.append, submit=lambda *a: submitted.append(a))
    hub = SimpleNamespace(turns=0, conductor=conductor)
    hub.to_face = lambda msg: AppHub.to_face(hub, msg)
    client = SimpleNamespace(device={"name": "Phone", "id": "d1"}, send=lambda m: None)
    AppHub.submit(hub, client, "what's for dinner")
    assert sent == [{"t": "chat", "who": "you", "text": "what's for dinner", "from": "phone"}]
    assert submitted and submitted[0][0] == "app"

    def boom(msg):
        raise RuntimeError("face gone")
    AppHub.to_face(SimpleNamespace(conductor=SimpleNamespace(send=boom)), {"t": "chat"})
    AppHub.to_face(SimpleNamespace(conductor=None), {"t": "chat"})
