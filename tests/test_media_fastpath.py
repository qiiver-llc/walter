"""Podcast / YouTube voice commands run on the fast path (no brain), and every
other request gets an instant cached acknowledgement at the transcript."""
import asyncio
import time

from walter import mediacmd
from walter.mediacmd import match

from test_barge_in import FakeBrain, make


def show(words):
    w = [x for x in words.lower().replace(",", "").split() if x not in ("the", "podcast")]
    title = "Morning Hour with Sam Rivers"
    return title if w and all(x in title.lower().split() for x in w) else None


def m(text, pod=False, vid=False):
    return match(text, podcast_active=pod, video_active=vid, find_show=show)


def test_play_a_followed_podcast():
    c = m("Walter, play Morning Hour.")
    assert c.argv == ["podcast", "play", "morning hour"] and c.say == "Playing Morning Hour with Sam Rivers."
    assert m("can you play morning hour please").argv[:2] == ["podcast", "play"]
    assert m("play the latest episode of morning hour").argv == ["podcast", "play", "morning hour"]
    assert m("play some jazz") is None             # not a followed show: the brain decides
    assert m("play Pat, Rivers") is None            # garbled: not every word is in the title


def test_play_on_youtube():
    c = m("play lofi hip hop on YouTube")
    assert c.argv == ["media", "youtube", "play", "lofi hip hop"] and "YouTube" in c.say
    assert m("put it on youtube") is None


def test_transport_only_while_something_plays():
    assert m("pause") is None and m("skip ahead") is None and m("stop") is None
    assert m("pause", pod=True).argv == ["podcast", "pause"]
    assert m("pause the video", vid=True).argv == ["media", "pause"]
    assert m("resume", pod=True).argv == ["podcast", "resume"]
    assert m("stop the podcast", pod=True).argv == ["podcast", "stop"]
    assert m("Walter stop", vid=True).argv == ["media", "stop"]
    assert m("skip 30 seconds", pod=True).argv == ["podcast", "skip", "30"]
    assert m("skip ahead two minutes", pod=True).argv == ["podcast", "skip", "120"]
    assert m("go back", pod=True).argv == ["podcast", "back", "15"]
    assert m("rewind 10 seconds", vid=True).argv == ["media", "back", "10"]
    assert m("next video", vid=True).argv == ["media", "next"]
    # both running, the named one wins
    assert m("pause the podcast", pod=True, vid=True).argv == ["podcast", "pause"]
    assert m("pause the video", pod=True, vid=True).argv == ["media", "pause"]
    # "play the podcast" with nothing running resumes the last one
    assert m("play the podcast").argv == ["podcast", "resume"]


def test_volume_and_speed_need_the_player_named():
    assert m("volume up", pod=True) is None        # that's my own speaker (quick.py)
    assert m("turn the podcast up", pod=True).argv[:2] == ["podcast", "volume"]
    assert m("video volume 40", vid=True).argv == ["media", "volume", "40"]
    assert m("speed 1.25", pod=True).argv == ["podcast", "speed", "1.25"]
    assert m("play at 1.5x speed", vid=True).argv == ["media", "speed", "1.5"]
    assert m("speed 9", pod=True) is None


def test_whats_playing_and_unclear_things():
    assert m("what's playing", pod=True).argv == ["podcast", "status"] and m("what's playing", pod=True).wait
    assert m("what's playing") is None
    assert m("pause and then tell me the weather", pod=True) is None
    assert m("what's the weather", pod=True) is None
    assert mediacmd.speakable("Playing: Morning Hour with Sam Rivers: Ep 12, at 12:03 of 1:02:00") == \
        "Playing Morning Hour with Sam Rivers, Ep 12, 12 minutes in."
    assert mediacmd.speakable("Nothing playing") == "Nothing playing."


def conductor(pod=False, vid=False):
    c, sent = make("off", brain=FakeBrain(["[happy] Hi."]))
    ran = []
    c.media_state = lambda which: pod if which == "podcast" else vid
    c.media_show = show
    c.media_runner = lambda argv: (ran.append(argv), (0, "Paused Ep 12"))[1]
    return c, ran


def test_conductor_runs_media_without_the_brain_and_without_an_ack():
    async def go():
        c, ran = conductor(pod=True)
        c.submit("voice", "Walter, pause the podcast.")
        await asyncio.sleep(0.2)
        assert ran == [["podcast", "pause"]]
        assert c.voice.said == ["Paused."]          # the confirmation, no "One sec."
        assert c.inbox.empty() and c.brain.asked == []
        assert c.request_counts["fast_path"] == 1 and c.request_counts["acks"] == 0
    asyncio.run(go())


def test_whats_playing_speaks_the_players_answer():
    async def go():
        c, ran = conductor(pod=True)
        c.media_runner = lambda argv: (0, "Playing: Morning Hour with Sam Rivers: Ep 12, at 0:20 of 50:00")
        c.submit("voice", "Walter what's playing")
        for _ in range(20):
            await asyncio.sleep(0.05)
            if c.voice.said:
                break
        assert c.voice.said == ["Playing Morning Hour with Sam Rivers, Ep 12, just started."]
    asyncio.run(go())


def test_failed_media_command_says_why():
    async def go():
        c, _ = conductor()
        c.media_runner = lambda argv: (1, "Couldn't find a video for 'zzz' (the results page is on the wall).")
        c.submit("voice", "play zzz on youtube")
        for _ in range(20):
            await asyncio.sleep(0.05)
            if len(c.voice.said) > 1:
                break
        assert c.voice.said[0] == "Okay, finding zzz on YouTube."
        assert c.voice.said[1].startswith("Couldn't find a video")
    asyncio.run(go())


def test_instant_ack_at_the_transcript_not_after_the_brain():
    """Measured with a fake brain that takes 2 s to its first word: the cached ack is
    queued for the voice the moment the transcript arrives (well under 300 ms)."""
    async def go():
        class SlowBrain(FakeBrain):
            async def ask(self, text, model=None):
                self.asked.append(text)
                await asyncio.sleep(2)
                yield ("tool", "Bash")
                yield ("text", "Sunny all weekend.")
                yield ("done", "")
        c, _ = make("off", brain=SlowBrain())
        stamps = []
        c.voice.say = lambda t: (stamps.append((time.monotonic(), t)), c.voice.said.append(t))
        task = asyncio.create_task(c.run())
        t0 = time.monotonic()
        c.submit("voice", "Walter, what's the weather looking like this weekend?")
        await asyncio.sleep(2.5)
        task.cancel()
        assert stamps and stamps[0][1] in c.acks.options
        assert stamps[0][0] - t0 < 0.3
        assert sum(1 for _, t in stamps if t in c.acks.options) == 1   # never a second ack
        assert "already said" in c.brain.asked[0]
    asyncio.run(go())


def test_no_ack_for_chat_or_short_things():
    async def go():
        c, _ = make("off")
        for text in ("thanks Walter", "good morning", "yes"):
            c._instant_ack(text)
        assert c.voice.said == []
        c.user_muted = True
        c._instant_ack("what's on my calendar tomorrow")
        assert c.voice.said == []
    asyncio.run(go())
