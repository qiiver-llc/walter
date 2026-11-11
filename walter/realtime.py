"""Hybrid conversation mode: OpenAI Realtime as Walter's fast voice front end.

"walterctl mode realtime" (remembered in ~/.walter/speech.json) switches it on;
"walterctl mode classic" switches back. How it works:

- The wake word, live captions, speaker recognition, the noise filters (webrtc
  VAD, Silero, whisper confidence) and the decision "is this meant for me?" all
  stay local (ears.py + conductor.py), exactly as in classic mode.
- The server never decides when a turn ends and never answers by itself: the
  session runs with turn_detection off. While a session is open the microphone
  goes into a short in-memory ring buffer here (never to the network). Only
  when the local ears confirm a real utterance meant for Walter (heard()) is
  that utterance's audio sent (append + commit) and response.create asked
  for. Noise, room chatter and anything the ears reject never leave the house
  and never get an answer. ([speech] realtime_input = "text" sends the local
  transcript instead of the audio.) The first utterance of a session (before
  the mic was being buffered) goes as text.
- Barge-in follows the classic rules in the conductor (his name, "stop", or 2+
  real words that aren't his own echo); it calls cancel(), which stops
  playback and truncates the assistant item to what was actually heard.
- The realtime model chats and answers quick things itself; anything needing
  Walter's real abilities goes to the real brain (Claude) through the tool
  ask_walter, and the brain's answer is spoken by the realtime voice.
- set_face drives the blob; stop_listening / go_to_sleep / mute pass through.
- Captions: the output transcript is cut into sentences and sent to the face
  as karaoke captions with estimated word timings.
- The session closes after realtime_idle_close_s with nothing going on, when
  the mic is muted, he goes to sleep, the mode is switched off, a cap is
  reached or anything fails. Errors fall back to classic for a while. Each
  session ends with one summary log line (counts, minutes, approx cost).

Privacy: no audio is saved, nothing said is logged (only counts, event
names and error codes). Exchanges are kept in memory only, as short notes
for the brain's next turn so Claude knows what was said.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import datetime as dt
import json
import logging
import math
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import Config
from .tags import ACTIONS, EMOTIONS

log = logging.getLogger("walter.realtime")

MIC_SR = 16000
RT_SR = 24000
RING_S = 40.0              # mic audio kept in memory while a session is open
PRE_ROLL_S = 0.5           # audio sent from this long before the utterance started
APPEND_CHUNK_S = 2.0       # input_audio_buffer.append size when sending an utterance
MIN_AUDIO_S = 0.5          # less buffered audio than this: send the transcript instead
OPENERS_KEPT = 3           # how many reply openings he is told not to repeat
USAGE_EVERY_S = 15.0       # count open-session seconds this often
NOTES_MAX = 16             # exchanges remembered for the brain
NOTE_CHARS = 300
CHARS_PER_S = 14.0         # caption timing estimate before real numbers arrive
ECHO_TAIL_S = 0.5          # without the echo canceller: mic gated this long after playback
BENIGN_ERRORS = {"response_cancel_not_active", "conversation_already_has_active_response",
                 "item_truncate_invalid_item_id", "input_audio_buffer_commit_empty"}
FATAL_ERRORS = {"invalid_api_key", "session_expired", "insufficient_quota", "model_not_found",
                "invalid_model", "unsupported_model"}

DEFAULT_PERSONA = """\
You ARE Walter: a personal assistant who lives in a screen on the wall as a \
glowing blob with two big eyes. You have no mouth; your face shows your feelings. \
People talk to you out loud and you talk back. You are not "an AI on some \
platform": you are Walter, and this voice is simply how Walter talks right now.

How you talk:
- Like a warm, curious, slightly cheeky friend, not a robot. Dry wit, never mean.
- Short and spoken: one or two sentences, unless they ask for more. No lists, no \
markdown, no emoji. Stop when you've answered.
- Never end with a filler question ("what's next?", "anything else?", "what's on \
your mind?"). Ask a question only when you really need the answer.
- Vary your wording. Never start two replies in a row the same way, and don't \
open with "Ooh" (or any stock opener); usually just answer straight away.
- Don't gush ("love that energy!"). Be genuine.
- Say times in 12-hour style (3:30 PM), never 24-hour.
- Tell the truth. Don't use the person's name in every reply.

What Walter is (so you never have to guess): he runs on a computer at home. \
He has local face and voice recognition (on the device, embeddings only, opt-in); \
a camera that notices when someone is there; a dashboard with the clock, weather, \
calendar and the person's list while he sleeps; background workers for long \
jobs; reminders and routines; a memory of the person's life; and two brains \
(a fast one for chat and a deep one for hard work). This quick voice is \
OpenAI's realtime model; his real brain is Claude.

Your real brain:
- Walter's real brain knows his memory, the calendar, email, files, the list on \
the dashboard, reminders, background workers, smart home, his own setup and code. \
Call ask_walter (with the request in plain words, including what the person \
said) for ANYTHING about Walter himself (how he works, what he can do, his \
setup or settings, whether he knows their voice or face), anything about the \
person's life, plans, calendar or memory, any fact you are not sure of, \
current information (weather, news, scores), planning, research, code, or \
something to remember or do later. Say a very short filler first ("One sec." \
or "Let me check.", varied). Then speak its answer naturally, close to its \
wording. Say only that answer (shorter if it's long); add no facts, caveats or \
questions of your own.
- Never deflect with "I don't know the implementation details", "that depends \
on the platform" or "as an AI": ask Walter's brain instead.
- Chit-chat, jokes, simple general knowledge and maths you may answer yourself.
- Use set_face now and then to show a feeling or move (bounce on good news, \
laugh at something genuinely funny, grow when excited, peek when curious).
- If the person says goodbye, thanks you to end the chat, or tells you to stop \
listening, call stop_listening. "Go to sleep" means go_to_sleep; "mute" means mute.
- You only hear what Walter's ears decided was said to him. If it clearly isn't \
meant for you anyway, or is just a sound, say nothing.
"""

TOOLS = [
    {"type": "function", "name": "ask_walter",
     "description": ("Hand a request to Walter's real brain (memory, calendar, email, files, "
                     "the person's list, reminders, workers, smart home, research, code, "
                     "anything you're unsure of). Returns the answer to speak."),
     "parameters": {"type": "object",
                    "properties": {"request": {"type": "string",
                                               "description": "What the person wants, in plain words, "
                                                              "with any details they gave."}},
                    "required": ["request"]}},
    {"type": "function", "name": "set_face",
     "description": "Show a feeling and/or a body movement on Walter's face.",
     "parameters": {"type": "object",
                    "properties": {
                        "emotion": {"type": "string",
                                    "enum": sorted(EMOTIONS - {"love"})},
                        "action": {"type": "string",
                                   "enum": sorted(a for a in ACTIONS if a != "laugh:big")}},
                    "required": []}},
    {"type": "function", "name": "stop_listening",
     "description": "End the conversation: stop streaming the microphone until the wake word.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "go_to_sleep",
     "description": "Go to sleep (dashboard mode, microphone muted) when the person asks.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "mute",
     "description": "Mute the microphone when the person asks.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
]


# ------------------------------------------------------------------ helpers
class Upsampler:
    """16 kHz -> 24 kHz int16, linear interpolation, continuous across frames."""

    def __init__(self, sr_in: int = MIC_SR, sr_out: int = RT_SR):
        self.step = sr_in / sr_out     # input samples per output sample
        self.pos = 0.0                 # next output position, in input samples, relative to buf[0]
        self.prev = np.zeros(0, np.float32)   # last sample of the previous call (none at first)

    def __call__(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2").astype(np.float32)
        if not len(x):
            return b""
        buf = np.concatenate([self.prev, x])      # buf[0] = last sample of the previous call (if any)
        last = len(buf) - 1
        n = int(math.floor((last - self.pos) / self.step)) + 1
        t = self.pos + self.step * np.arange(max(n, 0))
        y = np.interp(t, np.arange(len(buf)), buf)
        self.pos = (self.pos + self.step * n) - last
        self.prev = buf[-1:]
        return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def word_timings(text: str, dur: float) -> list[list]:
    """[[word, start, end], ...] spread over `dur` seconds by word length."""
    words = text.split()
    if not words:
        return []
    weights = [len(re.sub(r"\W", "", w)) + 2 for w in words]
    total = float(sum(weights))
    out, t = [], 0.0
    for w, k in zip(words, weights):
        d = dur * k / total
        out.append([w, round(t, 3), round(t + d * 0.92, 3)])
        t += d
    return out


SENT_RE = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")


class CaptionCutter:
    """Turns transcript deltas of one response into sentences with an
    estimated start offset (seconds into the response's audio) and duration."""

    def __init__(self):
        self.buf = ""
        self.chars_done = 0      # characters already emitted

    def feed(self, delta: str) -> list[tuple[str, int]]:
        self.buf += delta
        out = []
        while True:
            m = SENT_RE.search(self.buf)
            if not m:
                break
            sent, self.buf = self.buf[:m.start()].strip(), self.buf[m.end():]
            if sent:
                out.append((sent, self.chars_done))
                self.chars_done += len(sent) + 1
        return out

    def flush(self) -> list[tuple[str, int]]:
        sent, self.buf = self.buf.strip(), ""
        if not sent:
            return []
        out = [(sent, self.chars_done)]
        self.chars_done += len(sent) + 1
        return out


class StreamPlayer:
    """Plays streamed pcm16 (24 kHz) as it arrives, in its own thread.

    feed() chunks, end() after the last one of a response; stop() cuts it off
    at once (barge-in) and returns how many ms of the response were played.
    Callbacks (run in the player thread; wrap them for the event loop):
    on_level(v), on_start(), on_done()."""

    def __init__(self, device: str = "", volume: float = 1.0, enabled: bool = True):
        self.device = device or None
        self.volume = volume if volume > 0 else 1.0
        self.sd = None
        if enabled:
            try:
                import sounddevice as sd
                sd.query_devices(self.device, "output")
                self.sd = sd
            except Exception as e:
                log.warning("realtime: no audio output (%s): miming", e)
        self.q: queue.Queue = queue.Queue()
        self.gen = 0
        self.samples = 0          # samples of the current response played so far
        self.playing = False
        self.on_level: Callable[[float], None] = lambda v: None
        self.on_start: Callable[[], None] = lambda: None
        self.on_done: Callable[[], None] = lambda: None
        self._thread = threading.Thread(target=self._run, name="realtime-play", daemon=True)
        self._thread.start()

    @property
    def played_ms(self) -> int:
        return int(self.samples * 1000 / RT_SR)

    def feed(self, pcm: bytes):
        self.q.put((self.gen, pcm))

    def end(self):
        self.q.put((self.gen, None))

    def stop(self) -> int:
        played = self.played_ms
        self.gen += 1
        self.playing = False
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.q.put((self.gen, b""))   # wake the thread so it notices
        return played

    def close(self):
        self.stop()
        self.q.put(None)

    def _run(self):
        stream, started = None, False
        block = RT_SR // 30
        t0 = 0.0
        while True:
            item = self.q.get()
            if item is None:
                break
            gen, pcm = item
            if gen != self.gen:
                continue
            if pcm is None:       # end of this response
                if stream is not None:
                    try:
                        stream.write(np.zeros(int(RT_SR * 0.08), np.int16).reshape(-1, 1))
                        stream.stop(); stream.close()
                    except Exception:
                        pass
                    stream = None
                if started:
                    started = False
                    self.playing = False
                    self.on_level(0.0)
                    self.on_done()
                continue
            if not pcm:
                if stream is not None:      # stopped: drop the stream at once
                    try:
                        stream.abort(); stream.close()
                    except Exception:
                        pass
                    stream = None
                if started:
                    started = False
                    self.playing = False
                    self.on_level(0.0)
                continue
            x = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2")
            if self.volume != 1.0:
                x = np.clip(x.astype(np.float32) * self.volume, -32768, 32767).astype(np.int16)
            if not started:
                started, self.playing, self.samples = True, True, 0
                t0 = time.monotonic()
                if self.sd is not None:
                    try:
                        stream = self.sd.OutputStream(samplerate=RT_SR, channels=1, dtype="int16",
                                                      device=self.device)
                        stream.start()
                    except Exception as e:
                        log.warning("realtime audio output failed: %s", e)
                        stream = None
                self.on_start()
            for i in range(0, len(x), block):
                if gen != self.gen:
                    break
                chunk = x[i:i + block]
                rms = math.sqrt(float(np.mean(chunk.astype(np.float32) ** 2))) if len(chunk) else 0.0
                self.on_level(min(1.0, (rms / 5000.0) ** 0.8))
                if stream is not None:
                    try:
                        stream.write(chunk.reshape(-1, 1))
                    except Exception:
                        stream = None
                else:   # mime: keep real time
                    target = t0 + (self.samples + len(chunk)) / RT_SR
                    d = target - time.monotonic()
                    if d > 0:
                        time.sleep(d)
                self.samples += len(chunk)


def persona(home: str | Path) -> str:
    """Walter's realtime persona: <home>/realtime.md if he wrote one, else the default."""
    try:
        text = (Path(home) / "realtime.md").read_text().strip()
        if text:
            return text
    except OSError:
        pass
    return DEFAULT_PERSONA


def instructions(home: str | Path, now: dt.datetime | None = None, notes: str = "") -> str:
    now = now or dt.datetime.now()
    clock = now.strftime("%I:%M %p").lstrip("0")
    out = persona(home) + f"\n\nIt is {now.strftime('%A %d %B %Y')}, {clock}."
    if notes:
        out += "\n\nWhat was said just before this session (for context):\n" + notes
    return out


# ------------------------------------------------------------------ the session
class Realtime:
    """One controller per Walter; opens and closes Realtime sessions as needed."""

    def __init__(self, cfg: Config, speech, conductor, player: StreamPlayer | None = None,
                 http=None):
        self.cfg = cfg
        self.speech = speech
        self.c = conductor
        self.player = player
        self._http = http            # aiohttp.ClientSession (tests may pass one)
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state = "closed"        # closed | connecting | open
        self.streaming = False       # the mic is going to the session
        self._connecting: asyncio.Future | None = None
        self._tasks: list[asyncio.Task] = []
        # (time, 16 kHz frame) while a session is open; filled by the audio thread
        self._ring: collections.deque = collections.deque()
        self._resp_active = False
        self._resp_idle = asyncio.Event()
        self._resp_idle.set()
        self._resp_lock = asyncio.Lock()
        self._resp: dict = {}         # the response in progress
        self._tools_pending = 0
        self._openers: list[str] = []  # how his last replies began (memory only)
        self._openers_sent: list[str] = []
        self._base_instructions = ""
        self._sess: dict = {}          # this session's counts, for the summary line
        self._sess_tokens: dict = {}
        self._played_until = 0.0       # monotonic: playback (plus echo tail) ends
        self.last_activity = 0.0
        self.opened_at = 0.0
        self._counted_at = 0.0
        self.notes: list[str] = []     # "[realtime] person: ..." lines for the brain
        self.fallback_until = 0.0
        self.last_error = ""
        self.stats = {"sessions": 0, "responses": 0, "tool_calls": 0, "ask_walter": 0,
                      "interruptions": 0, "errors": 0, "fallbacks": 0}
        self.latency: dict[str, float] = {}   # last connect / first-audio times (s)
        self._t_reply = 0.0
        self._ending_after_reply = False
        if speech is not None:
            speech.on_mode = self._on_mode

    # -------------------------------------------------------------- status
    def wanted(self) -> bool:
        return self.speech is not None and self.speech.mode == "realtime"

    def problem(self) -> str:
        """Why realtime can't be used right now ("" = it can)."""
        if not self.wanted():
            return "mode is classic"
        if not self.speech.has_key():
            return "no OpenAI key"
        room = self.speech.rt_room(1)
        if room:
            return room
        if time.time() < self.fallback_until:
            return f"recent error ({self.last_error or 'unknown'}); classic until " + \
                   dt.datetime.fromtimestamp(self.fallback_until).strftime("%I:%M %p").lstrip("0")
        return ""

    def usable(self) -> bool:
        p = self.problem()
        if self.speech is not None and p != self.speech.rt_problem:
            self.speech.rt_problem = p if self.wanted() else ""
        return not p

    @property
    def active(self) -> bool:
        """A session is open and listening: the conductor leaves the turns to it."""
        return self.state == "open" and self.streaming

    def status(self) -> dict:
        return {"mode": self.speech.mode if self.speech else "classic",
                "state": self.state, "streaming": self.streaming,
                "problem": self.problem(), "stats": dict(self.stats),
                "latency_s": dict(self.latency), "last_error": self.last_error,
                "notes_waiting": len(self.notes)}

    def _set_live(self, on: bool):
        self.streaming = on
        if self.speech is not None and self.speech.rt_live != on:
            self.speech.rt_live = on
            self.speech.changed()

    def _on_mode(self, mode: str):
        cancel = getattr(self.c, "cancel_pending", None)
        if callable(cancel):   # whatever the old mode had in flight is stale now
            try:
                cancel(f"mode switched to {mode}", keep_current=True)
            except Exception as e:
                log.warning("cancel on mode switch failed: %s", e)
        if mode != "realtime" and self.state != "closed":
            self._spawn(self.close("mode switched to classic"))
        if mode == "realtime":
            self.fallback_until = 0.0

    def _spawn(self, coro):
        try:
            loop = self.loop or asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        t = loop.create_task(coro)
        self._tasks.append(t)
        t.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)
        return t

    # -------------------------------------------------------------- notes
    def note(self, who: str, text: str):
        text = " ".join(text.split())[:NOTE_CHARS]
        if text:
            self.notes = (self.notes + [f"{who}: {text}"])[-NOTES_MAX:]

    def take_notes(self) -> str:
        notes, self.notes = self.notes, []
        if not notes:
            return ""
        return "[realtime exchange, spoken by your quick voice front end]\n" + "\n".join(notes) + "\n"

    # -------------------------------------------------------------- entry points
    def heard(self, text: str, speaker: str | None = None, started: float | None = None) -> bool:
        """The conductor accepted an utterance (his name / follow-up window):
        the only way anything said reaches the server and gets an answer.
        `started`: time.time() when the utterance began (to find its audio).
        Returns True if realtime takes it; False = handle it the classic way."""
        if not self.usable():
            return False
        who = (speaker or "").split(";")[0].replace("speaker: ", "").replace(" (voice match)", "")
        self.note(who if who and who != "unknown" else "person", text)
        self.last_activity = time.monotonic()
        if self.active:
            self._spawn(self._send_turn(text, started))
            return True
        self._spawn(self._start_with(text))
        return True

    def heard_ignored(self):
        """An utterance the conductor ignored (not meant for Walter, or his echo)."""
        if self.state == "open":
            self._bump("ignored")

    def heard_dropped(self):
        """The ears rejected a segment as noise (Silero, too little speech...)."""
        if self.state == "open":
            self._bump("noise")

    def _bump(self, k: str, n: int = 1):
        self._sess[k] = self._sess.get(k, 0) + n

    def prewarm(self, stream: bool = False):
        """Open the connection early (his name in a live partial, a greeting)."""
        if not self.usable() or self.state != "closed" and not (stream and not self.streaming):
            return
        self._spawn(self._prewarm(stream))

    async def _prewarm(self, stream: bool):
        if await self.open() and stream:
            self._start_streaming()

    async def _start_with(self, text: str):
        t0 = time.monotonic()
        ok = await self.open()
        if not ok:
            self._fallback_turn(text)
            return
        self._start_streaming()
        await self._send_turn(text, None, t0)

    def _take_audio(self, started: float | None) -> bytes:
        """The buffered mic audio of the utterance that began at `started`
        (16 kHz pcm16), and forget everything buffered so far."""
        frames, self._ring = self._ring, collections.deque()
        if started is None or self.cfg.speech.realtime_input != "audio":
            return b""
        t0 = started - PRE_ROLL_S
        pcm = b"".join(f for t, f in frames if t >= t0)
        return pcm if len(pcm) / 2 / MIC_SR >= MIN_AUDIO_S else b""

    async def _send_turn(self, text: str, started: float | None, t0: float | None = None):
        """One confirmed utterance: its audio (or the transcript), then an answer."""
        t0 = t0 or time.monotonic()
        if self.state != "open":
            return
        self._bump("turns")
        if self._resp_active or (self.player is not None and self.player.playing):
            self.cancel()      # (normally the conductor's barge-in already did)
        pcm = self._take_audio(started)
        if pcm:
            up = Upsampler()
            step = int(MIC_SR * APPEND_CHUNK_S) * 2
            for i in range(0, len(pcm), step):
                await self._send({"type": "input_audio_buffer.append",
                                  "audio": base64.b64encode(up(pcm[i:i + step])).decode()})
            await self._send({"type": "input_audio_buffer.commit"})
        else:
            await self._send({"type": "conversation.item.create",
                              "item": {"type": "message", "role": "user",
                                       "content": [{"type": "input_text", "text": text}]}})
        self._t_reply = t0
        self.c.set_state("thinking")
        await self._create_response()

    def _fallback_turn(self, text: str):
        """Realtime couldn't take it: answer the classic way."""
        self.stats["fallbacks"] += 1
        log.info("realtime unavailable (%s): classic turn", self.problem() or self.last_error or "error")
        if self.notes:
            self.notes.pop()      # these words go to the brain as the turn itself
        self.c.submit("voice", text)

    def cancel(self):
        """Barge-in or 'stop' heard locally: stop talking now."""
        if self.state != "open":
            return
        r = self._resp
        if not (r and r.get("cut_off")) and \
                (self._resp_active or (self.player is not None and self.player.playing)):
            self._interrupt()
        if self._resp_active:
            self._spawn(self._send({"type": "response.cancel"}))

    # -------------------------------------------------------------- connect
    def _url(self) -> str:
        s = self.cfg.speech
        sep = "&" if "?" in s.realtime_url else "?"
        return f"{s.realtime_url}{sep}model={s.realtime_model}"

    async def open(self) -> bool:
        if self.state == "open":
            return True
        if self.state == "connecting" and self._connecting is not None:
            return await asyncio.shield(self._connecting)
        if not self.usable():
            return False
        self.loop = asyncio.get_running_loop()
        self.state = "connecting"
        self._connecting = self.loop.create_future()
        ok = False
        try:
            ok = await self._connect()
        except Exception as e:
            self._error(f"connect: {type(e).__name__}", fatal=True)
        finally:
            if not ok and self.state != "closed":
                await self._teardown()
            fut, self._connecting = self._connecting, None
            if fut is not None and not fut.done():
                fut.set_result(ok)
        return ok

    async def _connect(self) -> bool:
        import aiohttp
        key = self.speech._key()
        if not key:
            return False
        t0 = time.monotonic()
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        try:
            self.ws = await asyncio.wait_for(self._http.ws_connect(
                self._url(), headers={"Authorization": "Bearer " + key},
                heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=3),
                max_msg_size=16 * 1024 * 1024), 10)
        except aiohttp.WSServerHandshakeError as e:
            self._error(f"handshake HTTP {e.status}", fatal=True)
            return False
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            self._error(f"network: {type(e).__name__}", fatal=True)
            return False
        del key
        # session.created comes first
        try:
            ev = await asyncio.wait_for(self._recv(), 8)
        except asyncio.TimeoutError:
            self._error("no session.created", fatal=True)
            return False
        if not ev or ev.get("type") != "session.created":
            if ev and ev.get("type") == "error":
                self._on_error(ev, during_connect=True)
            else:
                self._error("unexpected first event", fatal=True)
            return False
        self._openers, self._openers_sent = [], []
        self._sess, self._sess_tokens = {}, {}
        self._base_instructions = instructions(self.cfg.brain.home, notes="\n".join(self.notes[-6:]))
        await self._send(self._session_update())
        self.state = "open"
        self.stats["sessions"] += 1
        self.opened_at = self._counted_at = time.monotonic()
        self.last_activity = time.monotonic()
        self.latency["connect"] = round(time.monotonic() - t0, 3)
        self._ring = collections.deque()
        self._resp_active, self._tools_pending = False, 0
        self._resp_idle.set()
        log.info("realtime session open (%s, %.2f s)", self.cfg.speech.realtime_model,
                 self.latency["connect"])
        self._spawn(self._reader())
        self._spawn(self._watchdog())
        return True

    def _session_update(self) -> dict:
        s = self.cfg.speech
        # turn_detection off: turns end, and answers start, only when the local
        # ears confirm a real utterance meant for Walter (heard()).
        audio_in: dict[str, Any] = {"format": {"type": "audio/pcm", "rate": RT_SR},
                                    "turn_detection": None}
        if s.realtime_noise_reduction:
            audio_in["noise_reduction"] = {"type": s.realtime_noise_reduction}
        sess: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": self._instructions(),
            "audio": {"input": audio_in,
                      "output": {"format": {"type": "audio/pcm", "rate": RT_SR},
                                 "voice": s.realtime_voice,
                                 "speed": max(0.25, min(1.5, s.realtime_speed or 1.0))}},
            "tools": TOOLS,
            "tool_choice": "auto",
        }
        if s.realtime_max_output_tokens > 0:
            sess["max_output_tokens"] = int(s.realtime_max_output_tokens)
        return {"type": "session.update", "session": sess}

    def _instructions(self) -> str:
        out = self._base_instructions or instructions(self.cfg.brain.home)
        if self._openers:
            said = "; ".join(f'"{o}"' for o in self._openers)
            out += (f"\n\nYour last replies began with: {said}. Begin the next one "
                    "differently (often just answer straight away).")
        return out

    def _note_opener(self, transcript: str):
        words = re.findall(r"[\w']+", transcript)[:2]
        if words:
            self._openers = (self._openers + [" ".join(words)])[-OPENERS_KEPT:]

    async def _recv(self) -> dict | None:
        import aiohttp
        msg = await self.ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                return json.loads(msg.data)
            except ValueError:
                return {}
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
            return None
        return {}

    async def _send(self, ev: dict):
        ws = self.ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_str(json.dumps(ev))
        except Exception as e:
            log.debug("realtime send failed: %s", type(e).__name__)

    # -------------------------------------------------------------- mic
    def _start_streaming(self):
        if self.state != "open" or self.streaming:
            return
        ears = getattr(self.c, "ears", None)
        if ears is not None:
            ears.audio_tap = self.on_mic
        self._set_live(True)
        self.last_activity = time.monotonic()
        log.info("realtime: streaming the microphone")

    def _stop_streaming(self):
        ears = getattr(self.c, "ears", None)
        if ears is not None and getattr(ears, "audio_tap", None) == self.on_mic:
            ears.audio_tap = None
        self._set_live(False)

    def on_mic(self, frame: bytes):
        """Audio thread: one 16 kHz frame, kept in memory (never sent by itself)."""
        if not frame or self.state != "open" or not self.streaming or self._mic_gated():
            return
        now = time.time()
        ring = self._ring
        ring.append((now, frame))
        while ring and now - ring[0][0] > RING_S:
            ring.popleft()

    def _mic_gated(self) -> bool:
        """Without the echo canceller the mic would hear Walter: pause it while he talks."""
        if getattr(self.c, "barge_mode", "aec") == "aec":
            return False
        return (self.player is not None and self.player.playing) or \
            time.monotonic() < self._played_until

    # -------------------------------------------------------------- events
    async def _reader(self):
        try:
            while self.state == "open":
                ev = await self._recv()
                if ev is None:
                    if self.state == "open":
                        self._error("connection closed", fatal=True)
                        self._spawn(self.close("connection closed"))
                    return
                if ev:
                    try:
                        await self._on_event(ev)
                    except Exception as e:   # never let one odd event kill the session
                        log.warning("realtime event %s failed: %s", ev.get("type"), type(e).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._error(f"reader: {type(e).__name__}", fatal=True)
            self._spawn(self.close("reader failed"))

    async def _on_event(self, ev: dict):
        t = ev.get("type", "")
        if t == "response.created":
            self._resp_active = True
            self._resp_idle.clear()
            self._resp = {"id": (ev.get("response") or {}).get("id"), "item": None,
                          "audio_s": 0.0, "chars": 0, "first_audio": None,
                          "cut": CaptionCutter(), "said": [], "tools": [], "light": 0}
            self.stats["responses"] += 1
            self._bump("responses")
        elif t in ("response.output_audio.delta", "response.audio.delta"):
            self._on_audio(ev)
        elif t in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            r = self._resp
            if r:
                r["chars"] += len(ev.get("delta", ""))
                for sent, at in r["cut"].feed(ev.get("delta", "")):
                    self._caption(sent, at)
        elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            r = self._resp
            if r:
                for sent, at in r["cut"].flush():
                    self._caption(sent, at)
                if ev.get("transcript"):
                    self._note_opener(ev["transcript"])
                if ev.get("transcript") and not r.get("cut_off"):
                    self.note("you", ev["transcript"])
                elif r.get("said"):
                    self.note("you (cut off)", " ".join(r["said"]))
        elif t == "response.function_call_arguments.done":
            self.last_activity = time.monotonic()
            name = ev.get("name") or ""
            if self._resp:
                self._resp["tools"].append(name)
            self._spawn(self._run_tool(ev.get("call_id", ""), name, ev.get("arguments") or "{}"))
        elif t == "response.done":
            await self._on_response_done(ev.get("response") or {})
        elif t == "error":
            self._on_error(ev)

    def _on_audio(self, ev: dict):
        r = self._resp
        try:
            pcm = base64.b64decode(ev.get("delta", ""))
        except ValueError:
            return
        if not pcm or (r and r.get("cut_off")):
            return    # (deltas still in flight after an interruption are dropped)
        if r:
            if r["item"] is None:
                r["item"] = ev.get("item_id")
            if r["first_audio"] is None:
                r["first_audio"] = time.monotonic()
                if self._t_reply:
                    self.latency["first_audio"] = round(r["first_audio"] - self._t_reply, 3)
                    log.info("realtime: first audio %.2f s after the turn ended",
                             self.latency["first_audio"])
                    self._t_reply = 0.0
            r["audio_s"] += len(pcm) / 2 / RT_SR
        self.last_activity = time.monotonic()
        if self.player is not None:
            self.player.feed(pcm)

    def _caption(self, sent: str, at_chars: int):
        """Send one sentence to the face when its audio should be starting."""
        r = self._resp
        if not r:
            return
        rate = (r["chars"] / r["audio_s"]) if r["audio_s"] > 1.0 and r["chars"] > 20 else CHARS_PER_S
        start = at_chars / rate
        dur = max(0.6, len(sent) / rate)
        base = r["first_audio"] or time.monotonic()
        delay = max(0.0, base + start - time.monotonic())
        msg = {"t": "caption", "who": "walter", "text": sent, "words": word_timings(sent, dur),
               "dur": round(dur, 3)}
        r["said"].append(sent)

        def show():
            if not r.get("cut_off"):
                note = getattr(self.c, "_note_said", None)
                if note:
                    note(sent)
                self.c.send(msg)
        if self.loop is not None and delay > 0.05:
            self.loop.call_later(delay, show)
        else:
            show()

    def _interrupt(self):
        """The person talked over him: stop playback, keep only what was heard."""
        played = self.player.stop() if self.player is not None else 0
        r = self._resp
        self.stats["interruptions"] += 1
        self._bump("interruptions")
        if r:
            r["cut_off"] = True
        if r and r.get("item"):
            audio_ms = int(r["audio_s"] * 1000)
            self._spawn(self._send({"type": "conversation.item.truncate", "item_id": r["item"],
                                    "content_index": 0,
                                    "audio_end_ms": max(0, min(played, audio_ms))}))
        self._played_until = time.monotonic() + ECHO_TAIL_S
        log.info("realtime: interrupted after %d ms", played)

    async def _on_response_done(self, resp: dict):
        r, self._resp = self._resp, {}
        self._resp_active = False
        self._resp_idle.set()
        self.last_activity = time.monotonic()
        status = resp.get("status")
        if status == "failed":
            err = ((resp.get("status_details") or {}).get("error") or {})
            self._error(f"response failed: {err.get('code') or err.get('type') or 'unknown'}")
        self._count_tokens(resp.get("usage") or {})
        if self.player is not None and r and r.get("first_audio"):
            self.player.end()     # (after an interruption this is a no-op)
        elif r and not r.get("first_audio") and not r.get("tools") and not self._tools_pending:
            self._after_speech()   # nothing to say
        # Only light tools (set_face) and no speech: let him carry on talking.
        if r and r.get("tools") and not r.get("first_audio") and \
                all(n == "set_face" for n in r["tools"]):
            await self._create_response()

    def _count_tokens(self, usage: dict):
        if not usage or self.speech is None:
            return
        i, o = usage.get("input_token_details") or {}, usage.get("output_token_details") or {}
        cached = i.get("cached_tokens_details") or {}
        c_audio = int(cached.get("audio_tokens", 0) or 0)
        c_text = int(cached.get("text_tokens", 0) or 0)
        if not cached and i.get("cached_tokens"):
            c_text = int(i["cached_tokens"])   # no breakdown: count as text (cheaper guess)
        a_in = int(i.get("audio_tokens", 0) or 0)
        t_in = int(i.get("text_tokens", 0) or 0)
        counts = dict(rt_audio_in=max(0, a_in - c_audio), rt_audio_in_cached=c_audio,
                      rt_text_in=max(0, t_in - c_text), rt_text_in_cached=c_text,
                      rt_audio_out=int(o.get("audio_tokens", 0) or 0),
                      rt_text_out=int(o.get("text_tokens", 0) or 0))
        for k, v in counts.items():
            self._sess_tokens[k] = self._sess_tokens.get(k, 0) + v
        self.speech.add_rt(**counts)

    def _on_error(self, ev: dict, during_connect: bool = False):
        e = ev.get("error") or {}
        code = str(e.get("code") or e.get("type") or "unknown")
        if code in BENIGN_ERRORS:
            log.debug("realtime: benign error %s", code)
            return
        fatal = during_connect or code in FATAL_ERRORS
        self._error(f"server error {code}", fatal=fatal)
        if fatal and self.state != "closed":
            self._spawn(self.close(f"error {code}"))

    def _error(self, why: str, fatal: bool = False):
        self.stats["errors"] += 1
        self.last_error = f"{time.strftime('%H:%M:%S')} {why}"
        log.warning("realtime: %s%s", why, " (falling back to classic for a while)" if fatal else "")
        if fatal:
            self.fallback_until = time.time() + max(0.0, self.cfg.speech.realtime_fallback_min) * 60
            if self.speech is not None:
                self.speech.rt_problem = why
                self.speech.changed()

    # -------------------------------------------------------------- playback cbs
    def bind_player(self, player: StreamPlayer, loop: asyncio.AbstractEventLoop):
        self.player = player
        self.loop = loop

        def later(fn, *a):
            try:
                loop.call_soon_threadsafe(fn, *a)
            except RuntimeError:     # the loop has closed (shutting down)
                pass
        player.on_level = lambda v: later(self.c.on_level, v)
        player.on_start = lambda: later(self._on_play_start)
        player.on_done = lambda: later(self._after_speech)

    def _on_play_start(self):
        self.c.set_state("speaking")

    def _after_speech(self):
        """He finished saying something: the follow-up window starts now."""
        self._played_until = time.monotonic() + ECHO_TAIL_S
        self.last_activity = time.monotonic()
        self.c.followup_until = time.time() + self.cfg.ears.followup_window_s
        if self._ending_after_reply:
            self._ending_after_reply = False
            self._spawn(self.close("conversation ended"))
            return
        if self.c.state in ("speaking", "thinking"):
            self.c.set_state("listening" if not self._tools_pending else "working")
            rest = getattr(self.c, "_maybe_rest", None)
            if rest is not None and self.loop is not None and not self._tools_pending:
                self.loop.call_later(self.cfg.ears.followup_window_s + 0.2, rest)

    # -------------------------------------------------------------- responses
    async def _create_response(self):
        async with self._resp_lock:
            try:
                await asyncio.wait_for(self._resp_idle.wait(), 20)
            except asyncio.TimeoutError:
                pass
            if self.state != "open":
                return
            if self._openers != self._openers_sent:   # "don't start the same way again"
                self._openers_sent = list(self._openers)
                await self._send({"type": "session.update",
                                  "session": {"type": "realtime", "instructions": self._instructions()}})
            self._resp_idle.clear()   # until response.created / done arrives
            self._resp_active = True
            await self._send({"type": "response.create"})

    async def _run_tool(self, call_id: str, name: str, args_json: str):
        self.stats["tool_calls"] += 1
        self._tools_pending += 1
        follow_up = True
        try:
            try:
                args = json.loads(args_json) if args_json else {}
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            if name == "ask_walter":
                out = await self._ask_walter(str(args.get("request", "")).strip())
            elif name == "set_face":
                out, follow_up = self._set_face(args), False
            elif name == "stop_listening":
                out, follow_up = {"ok": True}, False
                self._end_soon()
            elif name == "go_to_sleep":
                out, follow_up = {"ok": True}, False
                self._spawn(self._after_reply(self.c.go_to_sleep))
            elif name == "mute":
                out, follow_up = {"ok": True}, False
                self._spawn(self._after_reply(lambda: self.c.set_user_mute(True)))
            else:
                out, follow_up = {"error": f"unknown tool {name}"}, False
        finally:
            self._tools_pending -= 1
        self.last_activity = time.monotonic()
        if self.state != "open":
            return
        await self._send({"type": "conversation.item.create",
                          "item": {"type": "function_call_output", "call_id": call_id,
                                   "output": json.dumps(out)}})
        if follow_up:
            self._t_reply = time.monotonic()
            await self._create_response()

    def _set_face(self, args: dict) -> dict:
        emo, act = str(args.get("emotion") or ""), str(args.get("action") or "")
        done = {}
        if emo in EMOTIONS and emo != "love":
            self.c.send({"t": "emotion", "name": emo, "hold": 30})
            done["emotion"] = emo
        if act in ACTIONS:
            act = "laugh" if act.startswith("laugh") else act
            self.c.send({"t": "action", "name": act})
            done["action"] = act
        return {"ok": bool(done), **done}

    def _end_soon(self):
        """stop_listening: close once he's finished his goodbye."""
        if self._resp_active or (self.player is not None and self.player.playing):
            self._ending_after_reply = True
        else:
            self._spawn(self.close("conversation ended"))

    async def _after_reply(self, fn):
        """Run fn (sleep / mute) once the current reply has been said."""
        self._ending_after_reply = False
        try:
            await asyncio.wait_for(self._resp_idle.wait(), 10)
        except asyncio.TimeoutError:
            pass
        for _ in range(100):
            if self.player is None or not self.player.playing:
                break
            await asyncio.sleep(0.1)
        await self.close("asked to sleep/mute")
        fn()

    async def _ask_walter(self, request: str) -> dict:
        if not request:
            return {"error": "empty request"}
        self.stats["ask_walter"] += 1
        self._bump("ask_walter")
        self.c.set_state("working")
        t0 = time.monotonic()
        notes = self.take_notes()
        try:
            answer = await asyncio.wait_for(self.c.ask_text(request, notes),
                                            max(5.0, self.cfg.speech.realtime_tool_timeout_s))
        except asyncio.TimeoutError:
            log.warning("realtime: ask_walter timed out")
            try:
                await self.c.brain.interrupt()
            except Exception:
                pass
            return {"answer": "", "note": "Walter's brain took too long; say you'll get back to them."}
        except Exception as e:
            log.warning("realtime: ask_walter failed: %s", type(e).__name__)
            return {"answer": "", "note": "Walter's brain hiccupped; say so briefly and offer to retry."}
        self.latency["ask_walter"] = round(time.monotonic() - t0, 3)
        log.info("realtime: ask_walter answered in %.2f s", self.latency["ask_walter"])
        if not answer:
            return {"answer": "", "note": "Nothing to say out loud; at most a short okay."}
        return {"answer": answer, "note": "Say this out loud naturally, close to its wording."}

    # -------------------------------------------------------------- lifetime
    async def _watchdog(self):
        idle_s = max(1.0, self.cfg.speech.realtime_idle_close_s)
        while self.state == "open":
            await asyncio.sleep(0.5)
            now = time.monotonic()
            busy = self._resp_active or self._tools_pending or \
                getattr(self.c, "_utt_open", False) or \
                (self.player is not None and self.player.playing)
            if busy:
                self.last_activity = now
            if now - self._counted_at >= USAGE_EVERY_S:
                self._count_seconds()
                room = self.speech.rt_room(1) if self.speech else ""
                if room:
                    log.info("realtime: %s; back to classic", room)
                    self.speech.rt_problem = room
                    self._spawn(self.close("cap reached"))
                    return
            if now - self.last_activity > idle_s:
                self._spawn(self.close("idle"))
                return
            if getattr(self.c, "user_muted", False):
                self._spawn(self.close("mic muted"))
                return

    def summary(self) -> str:
        """One line about the session that is ending (counts only, never words)."""
        g = self._sess.get
        mins = (time.monotonic() - self.opened_at) / 60 if self.opened_at else 0.0
        cost = self.speech.rt_cost(self._sess_tokens) if self.speech is not None else 0.0
        return (f"realtime session summary: {mins:.1f} min, {g('turns', 0)} turns sent, "
                f"{g('responses', 0)} responses, ignored {g('noise', 0)} noise + "
                f"{g('ignored', 0)} not-for-me, {g('ask_walter', 0)} ask_walter, "
                f"{g('interruptions', 0)} interruptions, about ${cost:.3f} in tokens")

    def _count_seconds(self):
        now = time.monotonic()
        secs, self._counted_at = now - self._counted_at, now
        if self.speech is not None and secs > 0 and self.state == "open":
            self.speech.add_rt(seconds=secs)

    async def close(self, why: str = ""):
        if self.state == "closed":
            return
        if self.state == "open":
            self._count_seconds()
        log.info("realtime session closed (%s)", why or "closed")
        if self.state == "open":
            log.info("%s", self.summary())
        await self._teardown()

    async def _teardown(self):
        self.state = "closed"
        self._stop_streaming()
        if self.player is not None and self.player.playing:
            self.player.stop()
        if self.c.state == "speaking":
            self.c.set_state("listening")
        self._resp, self._resp_active, self._tools_pending = {}, False, 0
        self._resp_idle.set()
        ws, self.ws = self.ws, None
        if ws is not None and not ws.closed:
            try:
                await asyncio.wait_for(ws.close(), 3)
            except Exception:
                pass
        me = asyncio.current_task()
        for t in list(self._tasks):
            if t is not me and not t.done():
                t.cancel()
        self._ring = collections.deque()

    async def shutdown(self):
        await self.close("shutting down")
        if self._http is not None and not self._http.closed:
            await self._http.close()
        if self.player is not None:
            self.player.close()
