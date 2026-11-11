"""The person's microphone mute (Space bar / walterctl mute), separate from
the self-echo mute the conductor manages while Walter speaks."""
from walter.ears import FRAME

from test_conductor import make
from test_live import make_ears, wait_idle

PCM = b"\1" * FRAME * 2


def test_user_mute_blocks_capture_independently_of_echo_mute():
    e, _ = make_ears()
    assert e._capture(PCM) == PCM
    e.user_muted.set()
    assert e._capture(PCM) == b"" and e.deaf()
    # Walter finishing speaking clears the echo mute, not the person's mute
    e.muted.set(); e.muted.clear()
    assert e._capture(PCM) == b""
    e.user_muted.clear()
    assert e._capture(PCM) == PCM and not e.deaf()
    e.stop()


def test_user_mute_drops_transcripts_and_partials():
    e, got = make_ears()
    e._open_utterance()
    e.user_muted.set()                     # muted while whisper was already busy
    e._maybe_partial([b"\0" * FRAME * 2] * 60)
    wait_idle(e)
    assert got["partial"] == []
    assert not e._process(b"\0" * FRAME * 2 * 90, 60, 30.0)
    assert got["text"] == []
    e.user_muted.clear()
    assert e._process(b"\0" * FRAME * 2 * 90, 60, 30.0)
    assert got["text"] == ["Hey Walter, what's the time?"]
    e.stop()


def test_conductor_toggle_broadcasts_and_survives_speech():
    c, sent = make()
    e, _ = make_ears()
    c.ears = e
    assert c.set_user_mute() is True and e.user_muted.is_set()
    assert sent[-1] == {"t": "mute", "on": True}
    c.set_state("speaking"); c.set_state("idle")   # echo mute on/off
    assert e.user_muted.is_set()
    assert c.set_user_mute(False) is False and not e.user_muted.is_set()
    assert c.set_user_mute(None) is True
    e.stop()
