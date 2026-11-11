"""Walter's ears: microphone -> voice activity detection -> Whisper (offline).

Everything runs locally on the laptop. Audio is only turned into text when
somebody is actually talking (WebRTC VAD gates it), and text is only sent to
Claude when it contains Walter's name or follows right after he spoke
(see conductor.py).

How he listens:
- WebRTC VAD starts a turn; to keep it going a frame must also be louder
  than the room's background noise (learned continuously), so a fan can't
  keep him "listening". Silero VAD and whisper's own confidence weed out
  noise that slips through.
- He transcribes *while* you talk (live partials), so he can react to his
  name at once and show what he's understood; the live transcript also
  decides how long a pause ends your turn (turn.py).
- Long turns are never cut off: every ~12 s, at a pause between words, the
  audio so far is transcribed and listening simply goes on; the pieces are
  stitched into one message when you finish.
- A turn in progress when Walter starts speaking is still delivered.
- Cloud ears (optional, speech.py, "walterctl ears openai"): whisper still
  runs locally first, as the privacy gate and for live partials. Only a
  finished turn that is meant for Walter (cloud_gate: his name, or the
  follow-up window) is sent to OpenAI as one short wav, and its transcript
  replaces the local one. Any error, timeout or cap keeps the local text.
- Who is talking (opt-in, [senses] recognise_voices): each finished turn of
  at least a second of speech also gets a speaker embedding (people.py,
  computed alongside whisper, in memory; the audio is never kept), matched
  against enrolled voices. The conductor labels the message with the name.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import math
import os
import queue
import re
import threading
import time
from typing import Callable

import numpy as np

from .config import Config
from .turn import end_of_turn

log = logging.getLogger("walter.ears")

SR = 16000
FRAME_MS = 30
FRAME = SR * FRAME_MS // 1000

# Whisper likes to "hear" these in silence / noise.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thank you.", "thanks for watching", "thank you for watching",
    "bye", "okay", "uh", "um", "hmm", "mm", "so", "the", ".", "you.", "i'm sorry",
    "bye bye", "thanks for listening", "thank you for listening", "thank you so much",
    "please subscribe", "like and subscribe", "see you next time", "ill see you next time",
    "see you in the next video", "subtitles by the amaraorg community", "music", "applause",
    "laughter", "silence", "oh", "ah", "huh", "mhm", "hm",
}


def clean_text(text: str) -> str:
    """Whisper output -> text worth acting on ('' if it's noise/hallucination)."""
    text = re.sub(r"\s+", " ", text).strip()
    norm = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if norm in HALLUCINATIONS or len(norm) < 2:
        return ""
    if re.fullmatch(r"[\[\(].*[\]\)]", text):   # "[Music]", "(wind blowing)"
        return ""
    return text

PARTIAL_MIN_MS = 900       # don't bother transcribing less than this for a live partial
EARLY_FINAL_MS = 300       # silence after which the final transcription starts early
PARTIAL_REST_S = 0.5       # breather between live partials (a whisper pass is ~1.5 s of CPU)
PARTIAL_SLACK_FRAMES = 5   # a partial ending this close to the last speech still counts as current
REUSE_WAIT_S = 20.0        # max wait for a background job whose result we want to reuse
CHUNK_WAIT_S = 60.0        # max wait for the pieces of a long turn

NOISE_HISTORY = 333        # ~10 s of frame loudness for the background estimate
NOISE_PERCENTILE = 20      # quiet end of that history = the room's background
NOISE_MIN_RMS = 30.0       # never assume a background quieter than this (int16 RMS)
RESCUE_MIN_FLOOR = 60.0    # Silero rescue: treat a quieter background as this (noise-suppressed mic)
SOUND_WINDOW_FRAMES = 8    # sound reports (for the presence camera): peak of ~240 ms...
SOUND_REPORT_RATIO = 1.5   # ...sent only when it is this many times the background
ECHO_HISTORY = 333         # ~10 s of Walter's own echo loudness (speech frames while he talks)
ECHO_MIN_FRAMES = 30       # until ~1 s of his echo has been heard, barge-in checks aren't gated
BARGE_RECENT_FRAMES = 20   # the energy gate judges the newest ~0.6 s...
BARGE_LOUD_FRAMES = 12     # ...and needs ~0.36 s of it loud (a word like "Walter", not one
                           # syllable: a check on just the onset hears nothing and blocks the next)


def frame_rms(f: bytes) -> float:
    a = np.frombuffer(f, np.int16).astype(np.float32)
    return math.sqrt(float(np.mean(a * a)))


def loud_seconds(pcm: bytes, floor: float, ratio: float) -> float:
    """Seconds of 30 ms frames at least `ratio` times louder than `floor`."""
    n = len(pcm) // (FRAME * 2)
    if n == 0:
        return 0.0
    a = np.frombuffer(pcm[:n * FRAME * 2], np.int16).astype(np.float32).reshape(n, FRAME)
    rms = np.sqrt(np.mean(a * a, axis=1))
    return float(np.count_nonzero(rms >= max(floor, RESCUE_MIN_FLOOR) * ratio)) * FRAME_MS / 1000


def load_whisper(name: str, compute: str, threads: int, nice: int = 0):
    """Load a WhisperModel at a lower CPU priority. CTranslate2 does the actual
    decoding in worker threads that the *loading* thread creates (and their
    OpenMP helpers), and Linux threads inherit their creator's niceness. So
    loading inside a niced thread makes every whisper pass run niced, wherever
    it's called from, while the caller (and the rest of Walter) stays at 0."""
    from faster_whisper import WhisperModel

    def load():
        return WhisperModel(name, device="cpu", compute_type=compute, cpu_threads=max(0, threads))
    if nice <= 0:
        return load()
    box: dict = {}

    def run():
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), min(19, nice))
        except Exception as e:  # not Linux / not allowed: load normally
            log.debug("whisper: could not lower priority: %s", e)
        try:
            box["m"] = load()
        except BaseException as e:
            box["e"] = e
    t = threading.Thread(target=run, name="ears-load", daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["m"]


def should_rescue(pcm: bytes, floor: float, echo: bool, e_cfg) -> tuple[bool, float]:
    """Silero heard no speech: is this segment loud enough that whisper should
    judge it anyway? (yes/no, seconds of loud audio). Never while Walter was
    talking (his echo residue is loud too) or without a background estimate."""
    if not e_cfg.silero_rescue or echo or floor <= 0:
        return False, 0.0
    if len(pcm) / (SR * 2) < e_cfg.silero_rescue_min_s:
        return False, 0.0
    loud = loud_seconds(pcm, floor, e_cfg.silero_rescue_ratio)
    return loud >= e_cfg.silero_rescue_loud_s, loud


HW_MUTE_ON_S = 0.8          # this long of perfect digital silence = the mic's own mute button
HW_MUTE_OFF_S = 0.3         # this long of real signal clears it again
HW_MUTE_MAX_LSB = 2         # a "dead" frame: every sample the same value, within +-2 LSB of zero


def dead_frame(f: bytes) -> bool:
    """True for perfect digital silence: every sample identical and (nearly) zero.

    A live microphone always has some noise or dither, even in a silent room
    (a USB speakerphone measured std ~0.6 LSB, peaks of 2-3), so 30 ms of identical
    samples only happens when the device itself mutes (or stops) the capture.
    """
    if not f:
        return False
    a = np.frombuffer(f[:len(f) // 2 * 2], np.int16)
    if a.size == 0:
        return False
    v = int(a[0])
    return abs(v) <= HW_MUTE_MAX_LSB and bool(np.all(a == v))


class HwMuteDetector:
    """Spots a microphone muted by its own button (no HID/ALSA event, just zeros).

    Feed it raw capture frames (before any echo canceller or noise gate); it turns
    `muted` on after HW_MUTE_ON_S of dead frames and off after HW_MUTE_OFF_S of
    real signal, and calls on_change(bool) on each flip (in the caller's thread).
    """

    def __init__(self, on_change: Callable[[bool], None] | None = None,
                 on_s: float = HW_MUTE_ON_S, off_s: float = HW_MUTE_OFF_S, sr: int = SR):
        self.on_change = on_change
        self.on_s, self.off_s, self.sr = on_s, off_s, sr
        self.muted = False
        self._dead = 0.0   # seconds of consecutive dead frames
        self._live = 0.0   # seconds of consecutive live frames

    def feed(self, f: bytes) -> bool:
        if not f:
            return self.muted
        dur = len(f) / 2 / self.sr
        if dead_frame(f):
            self._dead += dur
            self._live = 0.0
            if not self.muted and self._dead >= self.on_s - 1e-6:
                self._flip(True)
        else:
            self._live += dur
            self._dead = 0.0
            if self.muted and self._live >= self.off_s - 1e-6:
                self._flip(False)
        return self.muted

    def _flip(self, on: bool):
        self.muted = on
        if self.on_change is not None:
            try:
                self.on_change(on)
            except Exception as e:
                log.debug("hw mute callback failed: %s", e)


class NoiseFloor:
    """Adaptive estimate of the room's background loudness.

    Keeps the loudness of the last ~10 s of microphone frames and takes a low
    percentile: speech comes and goes, the fan/fridge/traffic hum doesn't, so
    the quiet end of the history is the background. A frame is only loud
    enough to be speech if it is `ratio` times above that.
    """

    def __init__(self, ratio: float):
        self.ratio = ratio
        self.hist: collections.deque[float] = collections.deque(maxlen=NOISE_HISTORY)
        self.floor = NOISE_MIN_RMS
        self._n = 0

    def add(self, rms: float):
        self.hist.append(rms)
        self._n += 1
        if self._n % 10 == 0 and len(self.hist) >= 30:
            self.floor = max(NOISE_MIN_RMS, float(np.percentile(self.hist, NOISE_PERCENTILE)))

    def loud_enough(self, rms: float) -> bool:
        if self.ratio <= 0 or len(self.hist) < 30:  # off, or still warming up
            return True
        return rms >= self.floor * self.ratio


class _Job:
    """One transcription: the open utterance's audio so far (partial or early
    final), or a finished piece ("chunk") of a long turn."""

    def __init__(self, uid: int, pcm: bytes, frames: int, final: bool, chunk: bool = False,
                 floor: float = 0.0, echo: bool = False, trimmed: bool = False):
        self.uid, self.pcm, self.frames, self.final, self.chunk = uid, pcm, frames, final, chunk
        # a barge-in check: only the newest few seconds were transcribed, so its
        # text is not the whole utterance (never reused as the final transcript)
        self.trimmed = trimmed
        self.barge = False      # a barge-in check while Walter talked
        self.floor, self.echo = floor, echo   # background level; captured while Walter talked?
        self.text: str | None = None
        self.notes: list[str] = []
        self.done = threading.Event()


class Ears:
    def __init__(self, cfg: Config, on_text: Callable[[str], None],
                 on_hear: Callable[[float], None], on_start: Callable[[], None],
                 on_drop: Callable[[], None] | None = None,
                 on_partial: Callable[[str], None] | None = None,
                 on_end: Callable[[], None] | None = None,
                 on_sound: Callable[[float], None] | None = None,
                 on_speaker: Callable[[dict | None], None] | None = None):
        self.cfg = cfg
        self.speech = None      # walter.speech.Speech (cloud transcription switch), set by main
        # cloud_gate(local_text) -> may this finished turn go to the cloud? (conductor)
        self.cloud_gate: Callable[[str], bool] | None = None
        # Who said the turn that's about to be delivered (a voice match dict, or
        # None = unknown); emitted just before on_text. Needs voice_id (people.VoiceId).
        self.on_speaker = on_speaker
        self.voice_id = None
        self._spk_pieces: list[bytes] = []   # audio of a long turn's finished pieces (memory only)
        self._spk_pool = None
        # Loudness relative to the room's background (a number, never audio),
        # reported a few times a second when something stands out; the
        # presence camera uses it to wake from sleep.
        self.on_sound = on_sound
        self.on_drop = on_drop  # an utterance that started was rejected as noise
        self.on_text = on_text
        self.on_hear = on_hear
        self.on_start = on_start
        self.on_partial = on_partial  # live words while the person is still talking
        self.on_end = on_end          # the person stopped; transcribing the whole thing now
        # The mic's own mute button (JLab GO Talk: no HID/ALSA event, just digital
        # silence). Seen in the raw capture; never touches user_muted. on_hw_mute(bool)
        # runs on the event loop when it flips.
        self.hw_muted = threading.Event()
        self.on_hw_mute: Callable[[bool], None] | None = None
        self._hw = HwMuteDetector(self._hw_flip)
        self.muted = threading.Event()       # Walter is talking (managed by the conductor)
        self.user_muted = threading.Event()  # the person muted the mic (Space bar / walterctl mute)
        # Walter is talking but barge-in is on (aec.py): keep listening, but don't
        # learn his echo as room noise, and check for interruptions sooner.
        self.speaking = threading.Event()
        # Realtime mode (realtime.py): every raw 16 kHz frame also goes here while a
        # session is streaming (never while the person has muted the mic). Called
        # in the audio thread, so it must be quick (it only queues the bytes).
        self.audio_tap: Callable[[bytes], None] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.model = None        # fast model: live partials (and finals if no final_model)
        self.final_model = None  # bigger, more accurate model for each turn's final transcript
        self.cfg_gen = 0         # bumped by reconfigure(): _run re-reads its [ears] timings
        # One whisper pass at a time: on a 2-core CPU two at once only slow each
        # other down, so a running final pauses live partials (and vice versa).
        self._whisper_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        # Live partials: a second thread transcribes the growing buffer.
        self._cv = threading.Condition()
        self._pending: _Job | None = None   # next job (newest wins)
        self._current: _Job | None = None   # job being transcribed right now
        self._latest: _Job | None = None    # newest job submitted for this utterance
        self._chunks: list[_Job] = []       # finished pieces of the open (long) utterance
        self._chunkq: list[_Job] = []       # pieces waiting for whisper (go before partials)
        self._uid = 0                       # utterance counter; stale results are ignored
        self._rest_until = 0.0              # no new live partial before this (monotonic)
        # (text, frames_covered) of the newest live partial of the open utterance
        self.last_partial: tuple[str, int] = ("", 0)
        # Timing of the turn being delivered (monotonic): speech_end, turn_end,
        # text. Set just before on_text; the conductor reads it (latency.py).
        self._turn_marks: dict[str, float] = {}
        self.last_timing: dict[str, float] | None = None
        # Silero rescue (see should_rescue): the room's background level and
        # whether Walter was talking during the open utterance (echo risk).
        self._floor = 0.0
        self._echo = False
        # Walter's own echo level: loudness of speech frames heard while he talks
        # (kept across replies, so the first sentence of the next one is gated too)
        self._echo_hist: collections.deque[float] = collections.deque(maxlen=ECHO_HISTORY)
        self._barge_check_at = 0.0   # monotonic time of the last barge-in check
        self.barge_skipped = 0       # barge-in checks skipped by the energy gate (stats)

    def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        if not self.cfg.ears.enabled:
            return False
        try:
            import sounddevice as sd  # noqa: F401
            import webrtcvad  # noqa: F401
            from faster_whisper import WhisperModel  # noqa: F401
        except Exception as e:
            log.warning("ears disabled, missing dependency: %s", e)
            return False
        try:
            import sounddevice as sd
            sd.query_devices(self.cfg.ears.device or None, "input")
        except Exception as e:
            log.warning("ears disabled, no microphone: %s", e)
            return False
        e_cfg = self.cfg.ears
        log.info("loading whisper model %s ...", e_cfg.whisper_model)
        self.model = load_whisper(e_cfg.whisper_model, e_cfg.whisper_compute, e_cfg.whisper_threads,
                                  e_cfg.whisper_nice)
        if e_cfg.final_model and e_cfg.final_model != e_cfg.whisper_model:
            log.info("loading whisper model %s for final transcripts ...", e_cfg.final_model)
            try:
                self.final_model = load_whisper(e_cfg.final_model, e_cfg.whisper_compute,
                                                e_cfg.whisper_threads, e_cfg.whisper_nice)
            except Exception as e:
                log.warning("final_model %s failed to load (%s); using %s for everything",
                            e_cfg.final_model, e, e_cfg.whisper_model)
        try:  # the first pass is slow (lazy init); do it now, not on the first words
            self._recognise(np.zeros(SR // 2, np.int16).tobytes())
            if self.final_model is not None:
                self._recognise(np.zeros(SR // 2, np.int16).tobytes(), final=True)
        except Exception as e:
            log.debug("whisper warm-up failed: %s", e)
        if self.voice_id is not None and not self.voice_id.load():
            self.voice_id = None
        self.loop = loop
        self._running = True
        self._thread = threading.Thread(target=self._run, name="ears", daemon=True)
        self._thread.start()
        # background whisper: live partials, early finals and the pieces of long turns
        threading.Thread(target=self._partial_worker, name="ears-partial", daemon=True).start()
        log.info("ears listening")
        return True

    def reconfigure(self, changed: set[str] | None = None) -> str:
        """Apply a reloaded [ears] config (cfg.ears was updated in place). Timings,
        thresholds and the VAD are re-read between turns; a changed whisper model
        loads in the background and is swapped in when ready. Returns a note."""
        changed = set(changed or ())
        self.cfg_gen += 1
        models = {"whisper_model", "final_model", "whisper_compute", "whisper_threads",
                  "whisper_nice"}
        if not (changed & models) or self.model is None:
            return ""
        threading.Thread(target=self._reload_models, name="ears-reload", daemon=True).start()
        return "loading the new whisper model in the background"

    def _reload_models(self):
        e = self.cfg.ears
        try:
            log.info("reloading whisper model %s ...", e.whisper_model)
            fast = load_whisper(e.whisper_model, e.whisper_compute, e.whisper_threads, e.whisper_nice)
            final = None
            if e.final_model and e.final_model != e.whisper_model:
                final = load_whisper(e.final_model, e.whisper_compute, e.whisper_threads,
                                     e.whisper_nice)
        except Exception as ex:
            log.warning("whisper reload failed (%s); keeping the old model", ex)
            return
        self.model, self.final_model = fast, final
        log.info("whisper model reloaded (%s%s)", e.whisper_model,
                 f", final {e.final_model}" if final is not None else "")

    def stop(self):
        self._running = False
        with self._cv:
            self._cv.notify_all()

    def _emit(self, fn, *args):
        if fn and self.loop:
            self.loop.call_soon_threadsafe(fn, *args)

    def _hw_flip(self, on: bool):
        (self.hw_muted.set if on else self.hw_muted.clear)()
        log.info("microphone %s by its own mute button (digital silence)",
                 "muted" if on else "unmuted")
        if self.on_hw_mute is not None:
            self._emit(self.on_hw_mute, on)

    def _raw_tap_device(self):
        """The real mic to watch for hardware mute when the ears listen through the
        echo canceller (its gated output must not count): the default input.
        None = watch the ears' own stream (it is the raw mic)."""
        e = self.cfg.ears
        if e.device and e.aec_device and e.device == e.aec_device:
            return "default"
        return None

    def deaf(self) -> bool:
        """Not listening right now: Walter is talking, or the mic is muted by the person."""
        return self.muted.is_set() or self.user_muted.is_set()

    def _capture(self, indata) -> bytes:
        """One microphone frame, or b"" (marker: captured while deaf, or while the
        mic's own mute button is on: no VAD/whisper work on digital silence)."""
        return b"" if self.deaf() or self.hw_muted.is_set() else bytes(indata)

    def _run(self):
        import sounddevice as sd
        import webrtcvad

        vad = webrtcvad.Vad(self.cfg.ears.vad_aggressiveness)
        noise = NoiseFloor(self.cfg.ears.noise_gate_ratio)
        frames: queue.Queue[bytes] = queue.Queue(maxsize=500)
        raw_tap = self._raw_tap_device()
        hw_stream = None
        if raw_tap is not None:
            # Echo canceller in use: watch the real mic for its mute button on a
            # second, tiny stream (only a zero check per 30 ms frame).
            def hw_cb(indata, n, t, status):
                self._hw.feed(bytes(indata))
            try:
                hw_stream = sd.RawInputStream(samplerate=SR, blocksize=FRAME, channels=1,
                                              dtype="int16", device=raw_tap, callback=hw_cb)
                hw_stream.start()
            except Exception as e:
                log.warning("hardware-mute watch unavailable on %s: %s", raw_tap, e)
                hw_stream = None

        def cb(indata, n, t, status):
            # Check mute at capture time: frames can sit in the queue while
            # whisper is busy, and must not be judged by a later unmute
            # (that is how Walter heard his own voice as user speech).
            if raw_tap is None:
                self._hw.feed(bytes(indata))
            f = self._capture(indata)
            tap = self.audio_tap
            if tap is not None and not self.user_muted.is_set():
                try:
                    tap(f or bytes(indata))
                except Exception:
                    pass
            try:
                frames.put_nowait(f)
            except queue.Full:
                pass

        try:
            self._listen_loop(sd, cb, frames, vad, noise)
        finally:
            if hw_stream is not None:
                try:
                    hw_stream.stop(); hw_stream.close()
                except Exception:
                    pass

    def _listen_loop(self, sd, cb, frames, vad, noise):
        e_cfg = self.cfg.ears
        seen_gen = self.cfg_gen
        end_frames = max(1, e_cfg.end_silence_ms // FRAME_MS)
        max_frames = int(e_cfg.max_utterance_s * 1000 / FRAME_MS)
        chunk_frames = max(1, int(e_cfg.chunk_s * 1000 / FRAME_MS))
        # Early final: once this much silence has passed, start transcribing the
        # whole utterance in the background, so the text is (nearly) ready by
        # the time end_silence_ms confirms the person has finished.
        # Adaptive end of turn (turn.py): from this much silence on, ask whether
        # the words so far sound finished; the answer sets how long to wait.
        first_check = end_frames
        if e_cfg.adaptive_end_of_turn:
            first_check = max(1, min(e_cfg.end_silence_ms, e_cfg.end_silence_complete_ms,
                                     e_cfg.end_silence_unfinished_ms) // FRAME_MS)
        early_frames = min(first_check - 1, max(1, EARLY_FINAL_MS // FRAME_MS))
        held = ""  # why the turn was kept open past end_silence_ms (for the log)
        pre = collections.deque(maxlen=10)      # 300 ms pre-roll
        window = collections.deque(maxlen=10)   # speech/non-speech history
        voiced: list[bytes] = []   # audio of the current piece of the turn
        in_speech = False
        silence = 0
        speech_frames = 0
        turn_frames = 0            # length of the whole turn (all pieces)
        peak, peak_n = 0.0, 0      # loudest frame of the current sound-report window
        with sd.RawInputStream(samplerate=SR, blocksize=FRAME, channels=1, dtype="int16",
                               device=e_cfg.device or None, callback=cb):
            while self._running:
                try:
                    f = frames.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self.deaf() or not f:
                    # Walter is talking (don't listen to himself), or the person muted the mic
                    if in_speech:
                        if (not self.user_muted.is_set()
                                and speech_frames * FRAME_MS >= max(300, e_cfg.min_speech_ms)):
                            # Walter started speaking mid-turn: what was said before
                            # that still counts (it was captured before he spoke).
                            self._finish_turn(voiced, silence, speech_frames, noise.floor)
                        else:
                            self._close_utterance(0)
                            self._emit_drop()
                    in_speech, voiced, silence, held, turn_frames = False, [], 0, "", 0
                    pre.clear(); window.clear()
                    continue
                if len(f) != FRAME * 2:
                    continue
                if seen_gen != self.cfg_gen and not in_speech:
                    # "walterctl config reload": take the new [ears] timings between turns
                    seen_gen = self.cfg_gen
                    end_frames = max(1, e_cfg.end_silence_ms // FRAME_MS)
                    max_frames = int(e_cfg.max_utterance_s * 1000 / FRAME_MS)
                    chunk_frames = max(1, int(e_cfg.chunk_s * 1000 / FRAME_MS))
                    first_check = end_frames
                    if e_cfg.adaptive_end_of_turn:
                        first_check = max(1, min(e_cfg.end_silence_ms, e_cfg.end_silence_complete_ms,
                                                 e_cfg.end_silence_unfinished_ms) // FRAME_MS)
                    early_frames = min(first_check - 1, max(1, EARLY_FINAL_MS // FRAME_MS))
                    noise.ratio = e_cfg.noise_gate_ratio
                    try:
                        vad.set_mode(e_cfg.vad_aggressiveness)
                    except Exception as e:
                        log.warning("ears: bad vad_aggressiveness: %s", e)
                rms = frame_rms(f)
                if not self.speaking.is_set():
                    noise.add(rms)
                if len(noise.hist) >= 30:
                    self._floor = noise.floor
                if self.on_sound is not None and len(noise.hist) >= 30:
                    peak = max(peak, rms)
                    peak_n += 1
                    if peak_n >= SOUND_WINDOW_FRAMES:
                        ratio = peak / max(noise.floor, NOISE_MIN_RMS)
                        if ratio >= SOUND_REPORT_RATIO:
                            self._emit(self.on_sound, round(ratio, 2))
                        peak, peak_n = 0.0, 0
                speech = vad.is_speech(f, SR)
                if speech and self.speaking.is_set():
                    self._echo_hist.append(rms)   # what his own voice sounds like in the mic
                if not in_speech:
                    # Starting is as sensitive as plain webrtcvad, so quiet or
                    # distant speech is never missed; Silero and whisper's own
                    # confidence weed out noise that gets through.
                    pre.append(f)
                    window.append(speech)
                    if sum(window) >= 6:
                        in_speech = True
                        voiced = list(pre)
                        turn_frames = len(voiced)
                        silence = 0
                        speech_frames = sum(window)
                        self._open_utterance()
                        self._echo = self.speaking.is_set()
                        self._emit(self.on_start)
                else:
                    # To *keep* going, a frame must also be clearly louder than
                    # the room. Steady hum (fan, fridge, traffic) fools webrtcvad
                    # and used to stretch utterances to the 20 s cap; the
                    # adaptive gate lets them end when the person stops.
                    speech = speech and noise.loud_enough(rms)
                    voiced.append(f)
                    turn_frames += 1
                    if self.speaking.is_set():
                        self._echo = True
                    if speech and held:
                        log.info("end of turn: kept listening, they resumed after a %.1fs pause (%s)",
                                 silence * FRAME_MS / 1000, held)
                        held = ""
                    silence = 0 if speech else silence + 1
                    if speech:
                        speech_frames += 1
                        self._emit(self.on_hear, min(1.0, rms / 4000))
                        self._maybe_partial(voiced)
                    elif silence == early_frames and e_cfg.live_partials and e_cfg.early_final:
                        self._early_final(voiced, len(voiced) - silence)
                    over = turn_frames >= max_frames
                    if over:
                        log.info("utterance hit max length (%.0fs), cutting it off "
                                 "(background level %.0f)", e_cfg.max_utterance_s,
                                 noise.floor)
                    elif silence >= first_check:
                        over, verdict, reason, src = self._turn_over(silence, len(voiced) - silence)
                        if over:
                            log.info("end of turn after a %.1fs pause: %s, %s [%s]",
                                     silence * FRAME_MS / 1000, verdict, reason, src)
                        elif not held and silence >= end_frames:
                            held = f"{verdict}: {reason} [{src}]"
                    if over:
                        held = ""
                        n_speech = speech_frames
                        audio_piece, sil = voiced, silence
                        in_speech, voiced, speech_frames, turn_frames = False, [], 0, 0
                        pre.clear(); window.clear()
                        self._finish_turn(audio_piece, sil, n_speech, noise.floor)
                    elif (len(voiced) >= chunk_frames
                          and ((speech and silence == 0 and len(voiced) >= chunk_frames * 1.6)
                               or 2 <= silence < first_check)):
                        # A long turn: transcribe this piece now (at a pause between
                        # words if there is one) and keep listening.
                        self._submit_chunk(voiced)
                        voiced, silence, held = [], 0, ""

    def _finish_turn(self, voiced: list[bytes], silence: int, speech_frames: int, floor: float):
        """The person finished their turn: transcribe the rest and deliver it."""
        now = time.monotonic()
        # frames are handled as they arrive, so the speech ended `silence` frames ago
        self._turn_marks = {"speech_end": now - silence * FRAME_MS / 1000, "turn_end": now}
        audio = b"".join(voiced)
        reuse = self._close_utterance(len(voiced) - silence)
        with self._cv:
            chunks, self._chunks = self._chunks, []
        self._emit(self.on_end)
        spk = self._speaker_job(self._spk_pieces + [audio], speech_frames)
        self._spk_pieces = []
        if not self._process(audio, speech_frames, floor, reuse, chunks, spk):
            self._emit_drop()

    # ------------------------------------------------------- who's talking
    SPEAKER_MIN_MS = 1000     # less speech than this says too little about the voice
    SPEAKER_WAIT_S = 3.0

    def _speaker_job(self, pieces: list[bytes], speech_frames: int):
        """Start the speaker embedding of a finished turn (runs while whisper works)."""
        vid = self.voice_id
        if vid is None or not vid.active() or speech_frames * FRAME_MS < self.SPEAKER_MIN_MS:
            return None
        if self._spk_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._spk_pool = ThreadPoolExecutor(1, thread_name_prefix="ears-speaker")
        return self._spk_pool.submit(vid.analyse, b"".join(pieces))

    def _speaker_done(self, job, text: str):
        """The turn has text: collect the speaker result (enrolment sample / match)."""
        try:
            res = job.result(timeout=self.SPEAKER_WAIT_S)
        except Exception as e:
            log.info("speaker embedding failed: %s", e)
            return
        if not res:
            return
        vid = self.voice_id
        if res.get("enrol") is not None and vid is not None and vid.enrol is not None:
            vid.enrol.feed(res["enrol"], text)
            log.info("voice enrolment: sentence %d heard (%d samples)", vid.enrol.heard, len(vid.enrol.embs))
        m = res.get("match")
        if m is not None:
            log.info("speaker: %s (score %.2f, needs %.2f, best %s, %.1fs)", m["name"] or "unknown",
                     m["score"], m["threshold"], m["candidate"], res.get("dur", 0))
            if self.on_speaker:
                self._emit(self.on_speaker, m)

    def _turn_over(self, silence: int, speech_end: int) -> tuple[bool, str, str, str]:
        """Has the person finished? (end, verdict, reason, transcript source)"""
        text, pending, src = None, False, "none"
        job = self._latest
        if job is not None and job.frames >= speech_end - PARTIAL_SLACK_FRAMES:
            if job.done.is_set():
                text, src = job.text or "", ("early final" if job.final else "partial")
            elif self.last_partial[0] and self.last_partial[1] >= speech_end - PARTIAL_SLACK_FRAMES:
                # the final model is still working: judge by the fast partial meanwhile
                text, src = self.last_partial[0], "partial (final pending)"
            else:
                pending, src = True, "pending"
        elif self.last_partial[0] and self.last_partial[1] >= speech_end - PARTIAL_SLACK_FRAMES:
            text, src = self.last_partial[0], "partial"
        elif job is not None or self.last_partial[0]:
            src = "stale partial"
        elif self._chunks and speech_end <= PARTIAL_SLACK_FRAMES:
            # a long turn's piece was just handed over at this pause: judge by it
            piece = self._chunks[-1]
            if piece.done.is_set():
                text, src = piece.text or "", "long-turn piece"
            else:
                pending, src = True, "long-turn piece pending"
        end, verdict, reason = end_of_turn(silence * FRAME_MS, text, self.cfg.ears, pending)
        if src != "none" and text is not None:
            src += f" {text[-40:]!r}"
        return end, verdict, reason, src

    def _emit_drop(self):
        if self.on_drop:
            self._emit(self.on_drop)

    # ------------------------------------------------------- live partials
    def _open_utterance(self):
        with self._cv:
            self._uid += 1
            self._pending = self._latest = None
            self._chunks, self._chunkq = [], []
            self.last_partial = ("", 0)
        self._spk_pieces = []

    def _submit_chunk(self, voiced: list[bytes]):
        """Hand a finished piece of a long turn to whisper; the next piece's
        partials/early finals start afresh (their frame counts are per piece)."""
        job = _Job(self._uid, b"".join(voiced), len(voiced), final=True, chunk=True,
                   floor=self._floor, echo=self._echo)
        if self.voice_id is not None:   # the speaker check only needs the last ~10-30 s
            self._spk_pieces = (self._spk_pieces + [job.pcm])[-2:]
        with self._cv:
            self._chunks.append(job)
            self._chunkq.append(job)
            self._pending = self._latest = None   # stale partial of this piece
            self.last_partial = ("", 0)
            self._cv.notify()
        log.debug("long turn: piece of %.1fs handed to whisper", len(voiced) * FRAME_MS / 1000)

    def _so_far(self, text: str = "") -> str:
        """What the person has said in this turn so far: finished pieces + `text`."""
        with self._cv:
            done = [j.text for j in self._chunks if j.done.is_set() and j.text]
        return " ".join(done + ([text] if text else [])).strip()

    def _close_utterance(self, speech_end: int) -> "_Job | None":
        """End the open utterance. Returns a background job that already covers
        all of its speech (so its text can be reused as the final transcript)."""
        with self._cv:
            job = self._latest
            reuse = job if job is not None and speech_end > 0 and job.frames >= speech_end else None
            if reuse is not None and reuse.trimmed:
                reuse = None  # a barge-in check heard only the last few seconds
            if reuse is not None and not reuse.final and self.final_model is not None:
                reuse = None  # a fast-model partial: the final model must still hear it
            if self._pending is not None and self._pending is not reuse:
                self._pending = None  # stale partial: nobody will see it
            # pieces of this turn still waiting stay queued: the final needs them
            self._uid += 1            # late partials of this utterance are not shown
            self._latest = None
        return reuse

    def _maybe_partial(self, voiced: list[bytes]):
        e_cfg = self.cfg.ears
        if not e_cfg.live_partials:
            return
        n = len(voiced)
        last = self._latest.frames if self._latest else 0
        # while Walter talks, look sooner and more often: is this a barge-in?
        over = self.speaking.is_set()
        first_ms = e_cfg.barge_in_min_ms if over else PARTIAL_MIN_MS
        every_ms = min(600, e_cfg.partial_interval_s * 1000) if over else e_cfg.partial_interval_s * 1000
        if (n * FRAME_MS < first_ms
                or (n - last) * FRAME_MS < every_ms
                or (not over and n * FRAME_MS > e_cfg.partial_max_s * 1000)):
            return
        if self._pending is not None or self._current is not None:
            return  # previous partial still running: skip, keeps CPU sane
        if over:
            self._barge_check(voiced)
            return
        if time.monotonic() < self._rest_until:
            return  # one just finished: a breather first
        self._submit(voiced, final=False)

    def echo_level(self) -> float:
        """Typical loudness of Walter's own voice in the mic (0 = not known yet)."""
        if len(self._echo_hist) < ECHO_MIN_FRAMES:
            return 0.0
        return float(np.median(np.fromiter(self._echo_hist, np.float32)))

    def _barge_check(self, voiced: list[bytes]):
        """Walter is talking: is someone talking over him? Transcribing the whole
        growing utterance (mostly his own echo) every second used to keep both
        cores busy and starve his voice, so: only the newest barge_in_window_s
        are transcribed, and only when that audio is clearly louder than his echo
        usually is (someone near the mic). Quieter audio still gets a check every
        barge_in_recheck_s, so a soft voice can interrupt too, just more slowly."""
        e_cfg = self.cfg.ears
        now = time.monotonic()
        lvl = self.echo_level()
        ratio = e_cfg.barge_in_energy_ratio
        loud = True
        if ratio > 0 and lvl > 0:
            bar = lvl * ratio
            loud = sum(frame_rms(f) >= bar for f in voiced[-BARGE_RECENT_FRAMES:]) >= BARGE_LOUD_FRAMES
            if not loud:
                recheck = e_cfg.barge_in_recheck_s
                if recheck <= 0 or now - self._barge_check_at < recheck:
                    self.barge_skipped += 1
                    return
        if not loud or ratio <= 0 or lvl <= 0:
            if now < self._rest_until:
                return  # no clear sign of someone talking: keep the breather
        win = max(1, int(e_cfg.barge_in_window_s * 1000 / FRAME_MS)) if e_cfg.barge_in_window_s > 0 else 0
        self._barge_check_at = now
        tail = voiced[-win:] if win else voiced
        self._submit(voiced, final=False, pcm=b"".join(tail), trimmed=len(tail) < len(voiced),
                     barge=True)

    def _early_final(self, voiced: list[bytes], speech_end: int):
        """Silence began: transcribe everything now, unless a job already in
        flight (or done) covers all the speech."""
        job = self._latest
        if job is not None and job.frames >= speech_end and (job.final or self.final_model is None):
            return
        self._submit(voiced, final=True)

    def _submit(self, voiced: list[bytes], final: bool, pcm: bytes | None = None,
                trimmed: bool = False, barge: bool = False):
        job = _Job(self._uid, b"".join(voiced) if pcm is None else pcm, len(voiced), final,
                   floor=self._floor if final else 0.0, echo=self._echo, trimmed=trimmed)
        job.barge = barge
        with self._cv:
            self._pending = self._latest = job  # newest wins
            self._cv.notify()

    def _partial_worker(self):
        while self._running:
            with self._cv:
                while self._pending is None and not self._chunkq and self._running:
                    self._cv.wait()
                if not self._running:
                    return
                if self._chunkq:  # pieces of a long turn always go first
                    job = self._chunkq.pop(0)
                else:
                    job, self._pending = self._pending, None
                self._current = job
            try:
                job.text, job.notes = self._recognise(job.pcm, final=job.final,
                                                      floor=job.floor, echo=job.echo)
            except Exception as e:  # never let the worker die
                job.text, job.notes = None, [f"whisper failed: {e}"]
            finally:
                job.pcm = b""
                with self._cv:
                    self._current = None
                self._rest_until = time.monotonic() + PARTIAL_REST_S
                if job.barge:   # the quiet-audio recheck interval counts from here
                    self._barge_check_at = time.monotonic()
                job.done.set()
            if job.chunk:
                for n in job.notes:
                    log.info("%s", n.replace("{dur}", f"{job.frames * FRAME_MS / 1000:.1f}s"))
                if job.text and job.uid == self._uid and not self.user_muted.is_set() and self.on_partial:
                    self._emit(self.on_partial, self._so_far())  # shown while they keep talking
                continue
            if job.text and job.uid == self._uid and not self.user_muted.is_set():
                self.last_partial = (job.text, job.frames)
                log.debug("partial (%.1fs): %s", job.frames * FRAME_MS / 1000, job.text)
                if self.on_partial:
                    self._emit(self.on_partial, self._so_far(job.text))

    # --------------------------------------------------------- transcribe
    def _process(self, pcm: bytes, speech_frames: int, floor: float,
                 reuse: "_Job | None" = None, chunks: "list[_Job] | None" = None, spk=None) -> bool:
        """Pre-filter an utterance, then transcribe it. True if text was emitted.
        `chunks`: the already-handed-over pieces of a long turn (pcm is the rest)."""
        dur = len(pcm) / (SR * 2)
        speech_ms = speech_frames * FRAME_MS
        if chunks:
            return self._transcribe(pcm, reuse, chunks, spk)
        if dur < 0.4:  # a click or a cough
            log.info("rejected %.1fs segment: too short", dur)
            return False
        if speech_ms < self.cfg.ears.min_speech_ms:
            log.info("rejected %.1fs segment: only %d ms of speech (min %d, background %.0f)",
                     dur, speech_ms, self.cfg.ears.min_speech_ms, floor)
            return False
        return self._transcribe(pcm, reuse, spk=spk)

    def _transcribe(self, pcm: bytes, reuse: "_Job | None" = None,
                    chunks: "list[_Job] | None" = None, spk=None) -> bool:
        dur = len(pcm) / (SR * 2)
        text, notes = None, []
        if reuse is not None and reuse.done.wait(REUSE_WAIT_S):
            # The background job already heard everything that was said
            # (only silence came after it), with identical settings.
            text, notes = reuse.text, reuse.notes
            log.debug("final transcript reused from %s job", "early-final" if reuse.final else "partial")
        elif not chunks or dur >= 0.4:   # after a long turn's pieces the rest may be just a breath
            text, notes = self._recognise(pcm, final=True, floor=self._floor, echo=self._echo)
        for n in notes:
            log.info("%s", n.replace("{dur}", f"{dur:.1f}s"))
        if chunks:
            pieces = []
            for j in chunks:
                if not j.done.wait(CHUNK_WAIT_S):
                    log.warning("long turn: a piece took too long to transcribe, skipped")
                elif j.text:
                    pieces.append(j.text)
            text = " ".join(pieces + ([text] if text else [])).strip()
            dur += sum(j.frames for j in chunks) * FRAME_MS / 1000
        if not text:
            return False
        if self.user_muted.is_set():  # muted while whisper was still working on it
            log.info("ignored (mic muted) (%.1fs): %s", dur, text)
            return False
        text = self._cloud_final(text, pcm, chunks)
        if not text:
            return False
        if self.user_muted.is_set():  # muted (or asleep) while the cloud call was out: stale
            log.info("ignored (mic muted during cloud transcription) (%.1fs)", dur)
            return False
        log.info("heard (%.1fs): %s", dur, text)
        self.last_timing = dict(self._turn_marks, text=time.monotonic())
        if spk is not None:
            self._speaker_done(spk, text)
        self._emit(self.on_text, text)
        return True

    def _cloud_final(self, text: str, pcm: bytes, chunks: "list[_Job] | None" = None) -> str:
        """Cloud ears: re-transcribe a finished turn with OpenAI if that is
        switched on and the local text says it's meant for Walter. Returns the
        text to deliver (the local one on any failure)."""
        sp = self.speech
        if sp is None or sp.stt != "openai":
            return text
        try:
            if self.cloud_gate is not None and not self.cloud_gate(text):
                return text          # room chatter: never leaves the house
        except Exception as e:
            log.debug("cloud gate failed: %s", e)
            return text
        audio = b"".join([j.pcm for j in (chunks or [])] + [pcm])
        got = sp.transcribe(audio, hint_prompt(self.cfg.ears.hotwords))
        if got is None:
            return text              # error / cap: the local transcript stands
        got = clean_text(got)
        if not got:
            log.info("stt openai heard nothing; keeping the local transcript")
            return text
        return got

    def transcribe_pcm(self, pcm: bytes) -> str | None:
        """Local final-quality transcript of audio recorded elsewhere (the
        companion app's microphone: 16 kHz int16 mono). None = nothing heard
        or no model loaded. Blocking: run it in an executor."""
        if self.model is None:
            return None
        text, notes = self._recognise(pcm, final=True)
        for n in notes:
            log.info("app audio: %s", n.replace("{dur}", f"{len(pcm) / (SR * 2):.1f}s"))
        return text

    def _recognise(self, pcm: bytes, final: bool = False, floor: float = 0.0,
                   echo: bool = False) -> tuple[str | None, list[str]]:
        """Whisper + noise filters. (text or None, log notes). No side effects,
        so it is safe to run from the partial worker too. `final` passes use
        beam search (beam_size), live partials the fast greedy decoder.
        `floor`/`echo` (finals only) allow the Silero rescue (should_rescue)."""
        e_cfg = self.cfg.ears
        model = (self.final_model or self.model) if final else self.model
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        kw = dict(language="en", beam_size=max(1, e_cfg.beam_size) if final else 1,
                  condition_on_previous_text=False, no_speech_threshold=0.6,
                  initial_prompt=hint_prompt(e_cfg.hotwords))
        hotwords = " ".join(w.strip() for w in e_cfg.hotwords if w and w.strip()) or None
        if e_cfg.silero_vad:
            kw.update(vad_filter=True, vad_parameters=dict(
                threshold=e_cfg.silero_threshold, min_silence_duration_ms=500, speech_pad_ms=300))
        with self._whisper_lock:
            t0 = time.monotonic()
            try:
                try:
                    segs, info = model.transcribe(audio, hotwords=hotwords, **kw)
                except TypeError:   # older faster-whisper without hotwords
                    segs, info = model.transcribe(audio, **kw)
                parts, dropped = [], []
                for s in segs:  # (lazy: the decoding happens while iterating)
                    why = segment_rejection(s, e_cfg)
                    if why:
                        dropped.append(f"{s.text.strip()!r} ({why})")
                    else:
                        parts.append(s.text)
            except Exception as e:
                return None, [f"whisper failed: {e}"]
            took, secs = time.monotonic() - t0, len(audio) / SR
        if final and secs > 0:
            log.info("whisper: %.1f s of audio in %.1f s%s", secs, took,
                     ("  (slow: try final_model = \"\" or whisper_model = \"tiny.en\")"
                      if took > secs else ""))
        if e_cfg.silero_vad and getattr(info, "duration_after_vad", None) == 0:
            rescue, loud = should_rescue(pcm, floor, echo, e_cfg) if final else (False, 0.0)
            if not rescue:
                return None, ["rejected {dur} segment: Silero VAD found no speech"]
            return self._rescue(model, audio, kw, hotwords, loud, floor)
        notes = [f"rejected whisper segment: {d}" for d in dropped]
        raw = " ".join(p.strip() for p in parts).strip()
        text = clean_text(raw)
        if not text:
            if raw:
                notes.append(f"rejected {{dur}} segment: likely hallucination {raw!r}")
            return None, notes
        return text, notes


    def _rescue(self, model, audio, kw: dict, hotwords, loud: float,
                floor: float) -> tuple[str | None, list[str]]:
        """Silero heard no speech, but the segment is loud: one whisper pass
        without Silero, kept only if whisper itself is fairly sure it's speech."""
        e_cfg = self.cfg.ears
        # no prompt or hotwords: on marginal audio whisper likes to echo them back
        # ("Hey Walter"), which would wake him up
        kw = {k: v for k, v in kw.items() if k not in ("vad_filter", "vad_parameters", "initial_prompt")}
        hotwords = None
        head = (f"Silero VAD found no speech in {{dur}} segment, but {loud:.1f}s of it is "
                f"{e_cfg.silero_rescue_ratio:g}x above background {floor:.0f}: asked whisper anyway")
        with self._whisper_lock:
            try:
                try:
                    segs, _ = model.transcribe(audio, hotwords=hotwords, **kw)
                except TypeError:
                    segs, _ = model.transcribe(audio, **kw)
                parts, dropped = [], []
                for s in segs:
                    why = segment_rejection(s, e_cfg)
                    if not why and s.no_speech_prob > e_cfg.silero_rescue_no_speech:
                        why = f"no_speech_prob {s.no_speech_prob:.2f} (rescue)"
                    (dropped if why else parts).append(f"{s.text.strip()!r} ({why})" if why else s.text)
            except Exception as e:
                return None, [f"{head}; whisper failed: {e}"]
        text = clean_text(" ".join(p.strip() for p in parts).strip())
        if not text:
            why = "; ".join(dropped) if dropped else "nothing"
            return None, [f"{head}; whisper agrees, no speech ({why})"]
        return text, [f"{head}; rescued {text!r}"]


BASE_PROMPT = "Walter is a friendly wall buddy. Hey Walter."


def hint_prompt(hotwords: list[str]) -> str:
    """Whisper's initial prompt: the usual greeting plus the vocabulary hints,
    so names come out spelled right ("Siobhan", not "Shivon")."""
    words = [w.strip() for w in hotwords if w and w.strip() and w.strip().lower() != "walter"]
    return BASE_PROMPT + (f" Names: {', '.join(words)}." if words else "")


def segment_rejection(s, e_cfg) -> str:
    """Why a whisper segment looks like noise rather than speech ("" = keep it)."""
    if s.no_speech_prob > e_cfg.max_no_speech_prob:
        return f"no_speech_prob {s.no_speech_prob:.2f}"
    if s.avg_logprob < e_cfg.min_avg_logprob:
        return f"avg_logprob {s.avg_logprob:.2f}"
    if s.compression_ratio > e_cfg.max_compression_ratio:
        return f"compression_ratio {s.compression_ratio:.1f}"
    return ""
