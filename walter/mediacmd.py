"""Podcast / YouTube voice commands on the fast path (no brain round-trip).

`match(text, ...)` turns a clear, whole-utterance command ("play The Daily",
"pause", "skip 30 seconds", "play lofi beats on YouTube", "what's playing")
into a MediaCmd: the `walterctl` arguments to run plus a one-line spoken
confirmation. Conservative on purpose: anything unclear returns None and the
brain handles it. Bare transport words ("pause", "skip", "stop") only count
while a podcast or video is actually active.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import quick


@dataclass
class MediaCmd:
    argv: list[str]                 # walterctl arguments, e.g. ["podcast", "pause"]
    say: str                        # one-line confirmation ("" = speak the command's output)
    wait: bool = False              # run before speaking and say its output (status)
    note: str = ""                  # for the brain's context
    extra: dict = field(default_factory=dict)


NAME_RE = re.compile(r"\b(?:hey\s+|ok(?:ay)?\s+)?walter\b", re.I)


def _clean(text: str, wake_re=None) -> str:
    t = quick.strip_name(text, wake_re or NAME_RE).strip()
    t = re.sub(r"[.!?,]+$", "", t).strip()
    t = re.sub(r"^(?:(?:can|could|would|will) you\s+|please\s+|go ahead and\s+|just\s+)+", "", t, flags=re.I)
    t = re.sub(r"\s+please$", "", t, flags=re.I)
    return t.strip()


NUM = quick.NUM
POD = r"(?:the\s+)?(?:podcast|episode|show)"
VID = r"(?:the\s+)?(?:video|youtube(?:\s+video)?)"
ANY = r"(?:the\s+)?(?:podcast|episode|show|video|youtube|music|audio)"
STATUS_RE = re.compile(r"(?:what's|what is|whats)\s+(?:this\s+|currently\s+)?(?:playing|on)(?:\s+right\s+now|\s+now)?|"
                       r"what(?:'s| is) this (?:podcast|episode|video|show)|"
                       r"what (?:am i|are we) (?:listening to|watching)", re.I)


def _secs(n: str | None, unit: str | None, default: int) -> int | None:
    if not n:
        return default
    v = quick._num(n)
    if v is None:
        return None
    return v * 60 if unit and unit.lower().startswith(("min", "m")) and not unit.lower().startswith("ms") else v


def match(text: str, wake_re=None, podcast_active: bool = False, video_active: bool = False,
          find_show=None, radio_active: bool | None = None, find_station=None) -> MediaCmd | None:
    """A media command if the whole utterance is one, else None.
    podcast_active / video_active / radio_active: is that player running right now
    (radio_active None = ask the radio player itself).
    find_show(words) -> show title or None: a followed podcast that matches.
    find_station(words) -> favourite radio station name or None (default: the radio favourites)."""
    if "\n" in text or len(text) > 160:
        return None
    t = _clean(text, wake_re)
    if not t or re.search(r"\b(and then|and also|after that|but|unless|if)\b", t, re.I):
        return None
    low = t.lower()
    if radio_active is None:
        radio_active = globals()["radio_active"]()
    rc = _radio(t, low, radio_active, podcast_active or video_active,
                find_station if find_station is not None else favorite_station)
    if rc is not None:
        return rc

    # --- play X on YouTube
    m = re.fullmatch(r"(?:play|put on|put|show me|find)\s+(.{2,80}?)\s+on\s+youtube", low)
    if m:
        q = m.group(1).strip()
        if re.fullmatch(r"(?:it|that|this|something|anything|a video)", q):
            return None
        return MediaCmd(["media", "youtube", "play", q], f"Okay, finding {q} on YouTube.",
                        note=f'started playing "{q}" on YouTube on the wall')

    # --- what's playing
    if STATUS_RE.fullmatch(low):
        if podcast_active:
            return MediaCmd(["podcast", "status"], "", wait=True, note="told them what's playing")
        if video_active:
            return MediaCmd(["media", "status"], "", wait=True, note="told them what's playing")
        return None   # nothing of ours is playing: maybe they mean the TV; the brain decides

    # --- play <followed podcast>
    m = re.fullmatch(r"(?:play|put on|start|listen to)\s+(?:the\s+)?(?:(latest|newest|new)\s+)?(?:episode\s+of\s+)?"
                     r"(.{2,60}?)(?:\s+podcast)?", low)
    if m and find_show is not None:
        words = m.group(2).strip()
        title = find_show(words) if words not in ("it", "that", "this", "the podcast", "podcast",
                                                  "something", "music", "some music") else None
        if title:
            return MediaCmd(["podcast", "play", words], f"Playing {title}.",
                            note=f'started the podcast "{title}"')

    which = None   # "podcast" | "video": what a transport command acts on
    if re.search(r"\b(podcast|episode|show)\b", low):
        which = "podcast" if podcast_active or not video_active else None
    elif re.search(r"\b(video|youtube)\b", low):
        which = "video" if video_active or not podcast_active else None
    elif podcast_active:
        which = "podcast"
    elif video_active:
        which = "video"
    if which is None:
        return None
    pod = which == "podcast"
    target = rf"(?:\s+{ANY})?(?:\s+for\s+(?:a|a sec|a second|a minute|now))?"

    # --- pause / resume / stop
    if re.fullmatch(rf"(?:pause|hold){target}|(?:pause|hold) it", low):
        return MediaCmd(["podcast", "pause"] if pod else ["media", "pause"], "Paused.",
                        note=f"paused the {which}")
    if re.fullmatch(rf"(?:resume|unpause|continue|keep playing|carry on|play){target}|"
                    rf"(?:resume|unpause|continue) it|(?:start|play) it again", low):
        if low == "play" and not (podcast_active or video_active):
            return None
        return MediaCmd(["podcast", "resume"] if pod else ["media", "resume"], "Carrying on.",
                        note=f"resumed the {which}")
    if re.fullmatch(rf"(?:stop|turn off|switch off|shut off|close){target}|stop (?:it|playing)", low):
        if not (podcast_active or video_active):
            return None
        return MediaCmd(["podcast", "stop"] if pod else ["media", "stop"], "Stopped.",
                        note=f"stopped the {which}")

    # --- skip / back
    unit = r"(seconds?|secs?|minutes?|mins?)"
    m = re.fullmatch(rf"(?:skip|skip ahead|skip forward|jump ahead|fast forward|go forward|forward)"
                     rf"(?:\s+(?:by\s+)?{NUM}\s+{unit})?", low)
    if m:
        s = _secs(m.group(1), m.group(2), 30 if pod else 10)
        if not s or s > 3600:
            return None
        return MediaCmd((["podcast", "skip", str(s)] if pod else ["media", "skip", str(s)]),
                        f"Skipped ahead {_say_secs(s)}.", note=f"skipped the {which} ahead {s} s")
    m = re.fullmatch(rf"(?:go back|back up|rewind|skip back|jump back)(?:\s+(?:by\s+)?{NUM}\s+{unit})?", low)
    if m:
        s = _secs(m.group(1), m.group(2), 15 if pod else 10)
        if not s or s > 3600:
            return None
        return MediaCmd((["podcast", "back", str(s)] if pod else ["media", "back", str(s)]),
                        f"Back {_say_secs(s)}.", note=f"rewound the {which} {s} s")
    if not pod and re.fullmatch(r"(?:next video|skip (?:this|the) video|play the next (?:one|video))", low):
        return MediaCmd(["media", "next"], "Next one.", note="skipped to the next video")

    # --- volume (only with the player named: plain "volume up" is my own speaker)
    m = re.fullmatch(rf"(?:turn|set)\s+{ANY}\s+(?:volume\s+)?(up|down)|{ANY}\s+volume\s+(up|down)", low)
    if m:
        d = m.group(1) or m.group(2)
        if pod:
            cur = int(_pod_volume())
            v = max(0, min(130, cur + (15 if d == "up" else -15)))
            return MediaCmd(["podcast", "volume", str(v)], "Turned it up." if d == "up" else "Turned it down.",
                            note=f"set the podcast volume to {v}")
        return MediaCmd(["media", "volume", d], "Turned it up." if d == "up" else "Turned it down.",
                        note=f"turned the video volume {d}")
    m = re.fullmatch(rf"(?:set\s+)?{ANY}\s+volume\s+(?:to\s+)?(\d{{1,3}})(?:\s*(?:percent|%))?", low)
    if m:
        v = int(m.group(1))
        if v > 100:
            return None
        return MediaCmd(["podcast" if pod else "media", "volume", str(v)], f"Volume {v}.",
                        note=f"set the {which} volume to {v}")

    # --- speed
    m = re.fullmatch(rf"(?:set\s+)?(?:the\s+)?(?:playback\s+)?speed\s+(?:to\s+)?(\d(?:\.\d{{1,2}})?)\s*(?:x|times)?|"
                     rf"play (?:it )?at\s+(\d(?:\.\d{{1,2}})?)\s*(?:x|times)?(?:\s+speed)?|"
                     rf"(normal) speed", low)
    if m:
        sp = 1.0 if m.group(3) else float(m.group(1) or m.group(2))
        if not 0.5 <= sp <= 3.0:
            return None
        return MediaCmd(["podcast" if pod else "media", "speed", f"{sp:g}"], f"Speed {sp:g}.",
                        note=f"set the {which} speed to {sp:g}x")
    return None


# --- radio ("play country radio", "play WNYC", "play <favourite>", "stop the music")
RADIO_GENRES = ("country", "classic country", "jazz", "smooth jazz", "classical", "rock", "classic rock",
                "oldies", "pop", "news", "talk", "christian", "gospel", "blues", "hip hop", "chill",
                "lofi", "ambient", "folk", "bluegrass", "80s", "90s", "70s", "60s", "sports", "dance",
                "soul", "metal", "alternative", "indie", "christmas", "npr", "public", "top 40")
_PLAY = r"(?:play|put on|start|turn on|switch on|listen to)\s+(?:some\s+|the\s+|a\s+)?"


def _radio(t: str, low: str, active: bool, other_active: bool, find_station) -> MediaCmd | None:
    # play the radio / play <genre> radio / play radio <station> / play the <genre> station
    m = re.fullmatch(_PLAY + r"radio", low)
    if m:
        return MediaCmd(["radio", "play"], "Radio on.", note="turned the radio back on (last station)")
    m = re.fullmatch(_PLAY + r"(.{2,40}?)\s+(?:radio|radio station|station)", low) or \
        re.fullmatch(r"(?:play|put on)\s+(?:the\s+)?radio\s+(?:station\s+)?(.{2,40})", low)
    if m:
        q = m.group(1).strip()
        if re.fullmatch(r"(?:it|that|this|something|anything|my|a|the|some)", q):
            return None
        say = f"Okay, {q} radio." if q in RADIO_GENRES else f"Okay, finding {q}."
        return MediaCmd(["radio", "play", q], say, note=f'started live radio for "{q}"')
    # play <favourite station> / play <CALLSIGN> (a US call sign spoken as capitals, e.g. WNYC)
    m = re.fullmatch(r"(?:play|put on|listen to)\s+(?:the\s+)?(.{2,40})", t, re.I)
    if m:
        words = m.group(1).strip()
        name = find_station(words) if find_station else None
        if name:
            return MediaCmd(["radio", "play", words], f"Playing {name}.", note=f'started the radio station "{name}"')
        if re.fullmatch(r"[KW][A-Z]{2,3}(?:[ -]?(?:FM|AM))?", words):
            return MediaCmd(["radio", "play", words], f"Okay, finding {words}.",
                            note=f'started live radio for "{words}"')
    if not active:
        return None
    # transport while the radio is on (named, or bare words when nothing else plays)
    named = re.search(r"\b(radio|music|station)\b", low)
    if not named and other_active:
        return None
    tgt = r"(?:\s+(?:the\s+)?(?:radio|music|station))?(?:\s+for\s+(?:a|a sec|a second|a minute|now))?"
    if re.fullmatch(rf"(?:stop|turn off|switch off|shut off|kill){tgt}|stop (?:it|playing)|turn (?:it|the (?:radio|music)) off", low):
        return MediaCmd(["radio", "stop"], "Radio off.", note="stopped the radio")
    if re.fullmatch(rf"(?:pause|hold|mute){tgt}|pause it", low):
        return MediaCmd(["radio", "pause"], "Paused.", note="paused the radio")
    if re.fullmatch(rf"(?:resume|unpause|continue|carry on|keep playing){tgt}|resume it", low):
        return MediaCmd(["radio", "resume"], "Back on.", note="resumed the radio")
    m = re.fullmatch(r"(?:turn|set)\s+(?:the\s+)?(?:radio|music)\s+(?:volume\s+)?(up|down)|"
                     r"(?:the\s+)?(?:radio|music)\s+volume\s+(up|down)|turn it (up|down)", low)
    if m and (named or m.group(3)):
        d = m.group(1) or m.group(2) or m.group(3)
        return MediaCmd(["radio", "volume", d], "Turned it up." if d == "up" else "Turned it down.",
                        note=f"turned the radio {d}")
    if STATUS_RE.fullmatch(low) or re.fullmatch(r"what (?:station|radio station) is this|what station(?: is it)?", low):
        return MediaCmd(["radio", "status"], "", wait=True, note="told them what's on the radio")
    return None


def radio_active() -> bool:
    try:
        from . import radio
        return radio.SOCK.exists() and radio.player_running()
    except Exception:
        return False


def favorite_station(words: str) -> str | None:
    try:
        from . import radio
        f = radio.find_favorite(words, radio.favorites(radio.load()))
        return f["name"] if f else None
    except Exception:
        return None


def _say_secs(s: int) -> str:
    if s % 60 == 0:
        m = s // 60
        return "a minute" if m == 1 else f"{m} minutes"
    return f"{s} seconds"


def _pod_volume() -> float:
    try:
        from . import podcast
        return float(podcast.load().get("volume", 100))
    except Exception:
        return 100.0


# ------------------------------------------------------------ live state
def podcast_active() -> bool:
    try:
        from . import podcast
        return podcast.player_running()
    except Exception:
        return False


def video_active() -> bool:
    try:
        from . import media
        return media.SOCK.exists() and bool(media.load().get("active"))
    except Exception:
        return False


def followed_show(words: str) -> str | None:
    """Title of a followed podcast matching these words (whole words), else None."""
    try:
        from . import podcast
        st = podcast.load()
        show = podcast.find_show(st, words)
    except Exception:
        return None
    if not show:
        return None
    # find_show is forgiving; on the fast path insist the words really name the show
    norm = lambda s: re.sub(r"[^a-z0-9 ]+", "", s.lower()).split()   # noqa: E731
    title, said = norm(show.get("title", "")), norm(words)
    said = [w for w in said if w not in ("the", "a", "podcast", "show")]
    if not said or not all(w in title for w in said):
        return None
    return show["title"]


def speakable(line: str) -> str:
    """A player's one-line output as a spoken sentence ("Playing: Show: Title, at
    12:03 of 1:02:00" -> "Playing Show: Title, 12 minutes in.")."""
    line = (line or "").strip()
    if not line:
        return ""
    m = re.match(r"(Playing|Paused): (.+?), at (\d+:)?(\d+):\d\d(?: of [\d:]+)?(.*)$", line)
    if m:
        h = int((m.group(3) or "0:")[:-1])
        mins = h * 60 + int(m.group(4))
        where = "just started" if mins == 0 else ("a minute in" if mins == 1 else f"{mins} minutes in")
        what = m.group(2).replace(": ", ", ", 1)
        return f"{m.group(1)} {what}, {where}."
    line = re.sub(r"https?://\S+", "", line).strip()
    return line if line.endswith((".", "!", "?")) else line + "."
