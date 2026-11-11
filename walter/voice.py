"""Walter's voice: text-to-speech (Piper, fully offline) + playback.

While audio plays we measure its loudness ~30 times a second and send it to
the face so Walter's body pulses in time with his words.

Fallbacks: espeak-ng if no Piper voice is installed, and a silent "mime"
mode if there is no sound card at all (the face still animates).

Cloud voice (optional, speech.py): with "walterctl voice openai" each
sentence is synthesised by OpenAI instead, with estimated word timings for
the karaoke captions; any failure, timeout or cap falls back to piper for
that sentence. Laughs and other sound effects always stay local.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import random
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .config import Config
from .tags import NEVER_SPLIT, SOFT_SPLIT, speech_text

log = logging.getLogger("walter.voice")

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"


def _smooth(n: int) -> int:
    """Smallest 2^a 3^b 5^c >= n (sizes the FFT handles fast)."""
    best, p2 = 2 * n + 2, 1
    while p2 < 2 * n:
        p3 = p2
        while p3 < 2 * n:
            p5 = p3
            while p5 < n:
                p5 *= 5
            best = min(best, p5)
            p3 *= 3
        p2 *= 2
    return best


def resample(x: np.ndarray, sr_in: float, sr_out: float) -> np.ndarray:
    """Band-limited resampling of a float mono signal (FFT method, no scipy needed).

    Unlike linear interpolation this neither aliases when shrinking nor dulls
    the highs when stretching, so consonants stay crisp after the pitch shift.
    """
    from fractions import Fraction
    x = np.asarray(x, np.float32)
    n = len(x)
    m = int(round(n * sr_out / sr_in))
    if n < 2 or m < 2 or m == n:
        return x.copy() if m == n else np.zeros(max(m, 0), np.float32)
    # Zero-pad (which also tames wrap-around at the ends) to a length whose FFT
    # is fast in both directions: npad = q*k, mpad = p*k for the ratio p/q.
    r = Fraction(sr_out / sr_in).limit_denominator(64)
    p, q = r.numerator, r.denominator
    k = _smooth(-(-(n + 512) // q))
    npad, mpad = q * k, p * k
    xp = np.zeros(npad, np.float32)
    xp[256:256 + n] = x
    X = np.fft.rfft(xp)
    Y = np.zeros(mpad // 2 + 1, np.complex128)
    c = min(len(X), len(Y))
    Y[:c] = X[:c]
    y = np.fft.irfft(Y, mpad) * (mpad / npad)
    start = int(round(256 * mpad / npad))
    return y[start:start + m].astype(np.float32)


# ------------------------------------------------------------ word timing
# Karaoke captions: each caption word gets a (start, end) time in seconds from
# the start of its clip, so the face can light words up as they're spoken.

READING_WPM = 190          # captions-only (muted) pace: a relaxed reading speed
_STRESS = set("ˈˌːˑ̃ ^$_")  # stress/length marks, space, piper's BOS/EOS/PAD
_PUNCT = set(",.;:!?…—–-\"'()[]")


def _units(phonemes) -> int:
    """Phoneme count, ignoring stress/length marks, spaces and punctuation."""
    return sum(1 for p in phonemes if p not in _STRESS and p not in _PUNCT)


def _pause_units(word: str) -> float:
    """Extra time (in phoneme units) a voice takes after a word's punctuation."""
    w = word.rstrip("\"')]")
    core = w.rstrip(".").lower()
    if w.endswith(".") and not w.endswith("..") and (
            "." in core or core in NEVER_SPLIT or core in SOFT_SPLIT or len(core) == 1):
        return 0.0  # "a.m.", "Nov.", "J." aren't pauses
    if w.endswith((".", "!", "?", "…")):
        return 4.0
    if w.endswith((",", ";", ":", "—", "–")):
        return 2.0
    return 0.0


def layout_words(words: list[str], units: list[float], t0: float, t1: float) -> list[list]:
    """Spread words over [t0, t1] in proportion to their length plus the pauses
    their punctuation implies. Returns [[word, start, end], ...]."""
    if not words:
        return []
    units = [max(0.5, u) for u in units]
    pauses = [_pause_units(w) for w in words[:-1]] + [0.0]
    total = sum(units) + sum(pauses)
    scale = max(0.0, t1 - t0) / total if total else 0.0
    out, t = [], t0
    for w, u, p in zip(words, units, pauses):
        out.append([w, round(float(t), 3), round(float(t + u * scale), 3)])
        t += (u + p) * scale
    return out


def map_words(words: list[str], units: list[float], spans: list[tuple[float, float, float]],
              counts: list[int] | None = None) -> list[list]:
    """Place caption words on the timeline of what was actually spoken.

    spans: (units, start, end) for each spoken word (from piper's alignments).
    The caption and spoken words needn't match one-to-one ("$3.50" is spoken as
    "3 dollars 50"), so both are laid out on a shared phoneme-count axis and
    each caption word takes the time of the stretch of speech it covers."""
    if not words:
        return []
    spans = [sp for sp in spans if sp[0] > 0]
    if not spans:
        return []
    if counts and sum(counts) == len(spans):
        # one-to-one: caption word i is exactly the next counts[i] spoken words
        out, j, prev = [], 0, spans[0][1]
        for w, c in zip(words, counts):
            if c:
                prev_s, prev = spans[j][1], spans[j + c - 1][2]
                out.append([w, round(float(prev_s), 3), round(float(prev), 3)])
            else:  # nothing spoken for it (a lone symbol): flash with its neighbour
                out.append([w, round(float(prev), 3), round(float(prev), 3)])
            j += c
        return out
    units = [max(0.5, u) for u in units]
    k = sum(sp[0] for sp in spans) / sum(units)  # caption units -> spoken units
    edges = np.cumsum([0.0] + [sp[0] for sp in spans])

    def at(u: float, end: bool) -> float:
        u = min(max(u, 0.0), edges[-1])
        # which spoken word: for an end position, the word the point closes
        i = int(np.searchsorted(edges, u, side="left" if end else "right")) - 1
        i = min(max(i, 0), len(spans) - 1)
        n, a, b = spans[i]
        return a + (u - edges[i]) / n * (b - a)

    out, u = [], 0.0
    for w, n in zip(words, units):
        s, e = at(u * k, False), at((u + n) * k, True)
        out.append([w, round(float(s), 3), round(float(max(s, e)), 3)])
        u += n
    return out


def reading_timings(text: str, wpm: float = READING_WPM) -> tuple[list[list], float]:
    """Word timings at a natural reading pace (captions-only mode)."""
    words = text.split()
    if not words:
        return [], 0.0
    units = [len(re.sub(r"\W", "", w)) + 1.0 for w in words]
    avg = sum(units) / len(units)
    dur = max(0.8, 60.0 / wpm * sum(u / avg for u in units)) + sum(
        _pause_units(w) * 0.04 for w in words[:-1])
    dur = round(dur, 3)
    return layout_words(words, units, 0.0, dur), dur


def speech_bounds(audio: np.ndarray, sr: int) -> tuple[float, float]:
    """Start and end (seconds) of the audible part of a clip."""
    if len(audio) == 0:
        return 0.0, 0.0
    hop = max(1, sr // 100)
    n = len(audio) // hop
    if n == 0:
        return 0.0, len(audio) / sr
    frames = np.abs(audio[:n * hop].astype(np.float32)).reshape(n, hop).max(axis=1)
    on = np.nonzero(frames > max(200.0, 0.05 * float(frames.max())))[0]
    if len(on) == 0:
        return 0.0, len(audio) / sr
    return on[0] * hop / sr, min(len(audio), (on[-1] + 1) * hop) / sr


def mime_audio(dur: float, sr: int = 16000) -> np.ndarray:
    """No sound, just a plausible loudness envelope so the face still 'talks'."""
    t = np.arange(int(sr * max(0.3, dur))) / sr
    env = (0.5 + 0.5 * np.sin(2 * np.pi * 4.2 * t)) * (0.6 + 0.4 * np.sin(2 * np.pi * 1.3 * t))
    return (env * 8000).astype(np.int16)


class Synth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.kind = "mime"
        self.piper = None
        self.aligned = False
        self._units_cache: dict[str, tuple[int, int]] = {}
        model = Path(cfg.voice.piper_model).expanduser()
        if model.exists():
            try:
                from piper import PiperVoice
                # A copy patched with piper.patch_voice_with_alignment, if there is
                # one, also reports how long each phoneme lasts: exact karaoke timing.
                patched = model.with_name(model.stem + ".aligned.onnx")
                if patched.exists():
                    self.piper = PiperVoice.load(str(patched), config_path=f"{model}.json")
                else:
                    self.piper = PiperVoice.load(str(model))
                self.kind = "piper"
            except Exception as e:  # pragma: no cover - depends on install
                log.warning("piper failed to load (%s); falling back", e)
        if self.kind == "mime" and shutil.which("espeak-ng"):
            self.kind = "espeak"
        # Kokoro (trial, [voice] engine = "kokoro"): the preferred local voice when it
        # loads; piper stays loaded as its fallback.
        self.kokoro = None
        self.kokoro_failures = 0
        if str(getattr(cfg.voice, "engine", "piper")).lower() == "kokoro":
            self.kokoro = self.load_kokoro(cfg.voice.kokoro_voice)
        log.info("tts engine: %s%s", self.kind, " (+ kokoro %s)" % cfg.voice.kokoro_voice
                 if self.kokoro is not None else "")

    def load_kokoro(self, voice: str):
        """A Kokoro voice, or None (logged) if it can't load. Also used by the demo."""
        try:
            from .kokoro import Kokoro
            t0 = time.monotonic()
            k = Kokoro(self.cfg.voice.kokoro_dir, voice, threads=self.cfg.voice.kokoro_threads,
                       speed=self.cfg.voice.kokoro_speed)
            log.info("kokoro %s loaded in %.1f s", voice, time.monotonic() - t0)
            return k
        except Exception as e:
            log.warning("kokoro failed to load (%s); the local voice stays piper", e)
            return None

    @property
    def local_engine(self) -> str:
        """The local voice a new reply starts in: "kokoro" or "piper"."""
        return "kokoro" if self.kokoro is not None else "piper"

    def kokoro_timed(self, text: str, kokoro=None,
                     voice: str | None = None) -> tuple[np.ndarray, int, list[list]]:
        """Like synth_timed, but in Kokoro (self.kokoro unless given; voice = another
        speaker of the same model, for the demo). The
        audio is resampled to piper's rate so every clip plays the same way. Raises
        on failure (the caller falls back to piper)."""
        k = kokoro or self.kokoro
        if k is None:
            raise RuntimeError("kokoro not loaded")
        words = text.split()
        spoken = speech_text(text) or text
        audio, sr = k.synth(spoken, voice=voice) if voice else k.synth(spoken)
        if not len(audio):
            raise RuntimeError("kokoro produced no audio")
        out_sr = int(getattr(getattr(self.piper, "config", None), "sample_rate", 0) or sr)
        if out_sr != sr:
            audio = resample(audio, sr, out_sr)
        vol = self.cfg.voice.volume if self.cfg.voice.volume > 0 else 1.0
        audio = (np.clip(audio * vol, -1.0, 1.0) * 32767).astype(np.int16)
        units = [self._word_units(w)[0] for w in words]
        return audio, out_sr, layout_words(words, units, *speech_bounds(audio, out_sr))

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        """Return (int16 mono samples, sample_rate)."""
        audio, sr, _ = self.synth_timed(text)
        return audio, sr

    def _word_units(self, word: str) -> tuple[int, int]:
        """(phonemes, spoken words) the voice will use for one caption word."""
        if word not in self._units_cache:
            n, k = 0, 0
            spoken = speech_text(word)
            if self.piper is not None and spoken:
                try:
                    for sent in self.piper.phonemize(spoken):
                        n += _units(sent)
                        k += len([g for g in "".join(sent).split(" ") if _units(g)])
                except Exception:
                    n, k = 0, 0
            if n == 0:
                n = len(re.sub(r"\W", "", spoken or word))
                k = len(spoken.split())
            if len(self._units_cache) > 5000:
                self._units_cache.clear()
            self._units_cache[word] = (n, k)
        return self._units_cache[word]

    def synth_timed(self, text: str) -> tuple[np.ndarray, int, list[list]]:
        """Speak a caption sentence: (int16 samples, sample_rate, word timings).

        The audio says speech_text(text) (numbers, abbreviations etc. made
        speakable); the timings are for the words of `text` itself, as shown."""
        words = text.split()
        if self.kind == "piper":
            from piper.config import SynthesisConfig
            pitch = self.cfg.voice.pitch if self.cfg.voice.pitch > 0 else 1.0
            # Speak faster by the pitch factor, then stretch back to normal speed:
            # the stretch lowers (or raises) the pitch while keeping the tempo.
            syn = SynthesisConfig(length_scale=self.cfg.voice.length_scale * pitch,
                                  volume=self.cfg.voice.volume)
            spoken = speech_text(text) or text
            chunks = list(self.piper.synthesize(spoken, syn_config=syn, include_alignments=True))
            if not chunks:
                return np.zeros(0, np.int16), 22050, []
            sr = chunks[0].sample_rate
            audio = np.concatenate([c.audio_float_array for c in chunks])
            spans = self._spans(chunks)
            stretch = 1.0
            if abs(pitch - 1.0) > 1e-3 and len(audio) > 1:
                n = len(audio)
                audio = resample(audio, pitch, 1.0)
                stretch = len(audio) / n
            audio = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            info = [self._word_units(w) for w in words]
            units = [n for n, _ in info]
            timed = []
            if spans:
                timed = map_words(words, units, [(u, a * stretch, b * stretch) for u, a, b in spans],
                                  [k for _, k in info])
            if not timed:
                timed = layout_words(words, units, *speech_bounds(audio, sr))
            return audio, sr, timed
        if self.kind == "espeak":
            wav = subprocess.run(["espeak-ng", "-v", "en-us", "-s", "165", "--stdout",
                                  speech_text(text) or text],
                                 capture_output=True, check=True).stdout
            with wave.open(io.BytesIO(wav)) as w:
                audio, sr = np.frombuffer(w.readframes(w.getnframes()), np.int16), w.getframerate()
            units = [len(re.sub(r"\W", "", w)) for w in words]
            return audio, sr, layout_words(words, units, *speech_bounds(audio, sr))
        # mime: no audio, but a plausible loudness envelope for the face
        timed, dur = reading_timings(text)
        return mime_audio(dur), 16000, timed

    @staticmethod
    def _spans(chunks) -> list[tuple[float, float, float]]:
        """(phoneme units, start s, end s) of each spoken word, from piper's
        alignments; [] if the voice model doesn't report them."""
        spans: list[tuple[float, float, float]] = []
        offset = 0
        for c in chunks:
            al = c.phoneme_alignments
            sr = c.sample_rate
            if not al:
                return []
            total = sum(a.num_samples for a in al) or 1
            k = len(c.audio_float_array) / total  # alignments vs real length
            pos = 0.0
            cur_n, cur_a, cur_b = 0, None, None
            for a in al:
                dur = a.num_samples * k
                n = _units(a.phoneme)
                if n:
                    if cur_a is None:
                        cur_a = pos
                    cur_n += n
                    cur_b = pos + dur
                elif a.phoneme in (" ", "^", "$") or a.phoneme in _PUNCT:
                    if cur_n:
                        spans.append((cur_n, (offset + cur_a) / sr, (offset + cur_b) / sr))
                    cur_n, cur_a, cur_b = 0, None, None
                pos += dur
            if cur_n:
                spans.append((cur_n, (offset + cur_a) / sr, (offset + cur_b) / sr))
            offset += len(c.audio_float_array)
        return spans


class Sfx:
    """Short recorded sound effects (e.g. laughs) from sounds/<group>/*.wav.

    "laugh" picks a random soft-*.wav, "laugh:big" a random big-*.wav (falling
    back to any clip in the group). Never the same clip twice in a row.
    """

    def __init__(self, root: Path | str = SOUNDS_DIR):
        self.root = Path(root)
        self._cache: dict[Path, tuple[np.ndarray, int]] = {}
        self._last: dict[str, Path] = {}

    def candidates(self, name: str) -> list[Path]:
        group, _, intensity = name.partition(":")
        files = sorted((self.root / group).glob("*.wav"))
        prefix = "big-" if intensity == "big" else "soft-"
        chosen = [f for f in files if f.name.startswith(prefix)]
        return chosen or files

    def pick(self, name: str) -> tuple[np.ndarray, int] | None:
        files = self.candidates(name)
        if not files:
            return None
        group = name.partition(":")[0]
        if len(files) > 1 and self._last.get(group) in files:
            files = [f for f in files if f != self._last[group]]
        f = random.choice(files)
        self._last[group] = f
        if f not in self._cache:
            try:
                with wave.open(str(f)) as w:
                    if w.getsampwidth() != 2 or w.getnchannels() != 1:
                        raise ValueError("need mono 16-bit wav")
                    self._cache[f] = (np.frombuffer(w.readframes(w.getnframes()), np.int16),
                                      w.getframerate())
            except Exception as e:
                log.warning("bad sound %s: %s", f, e)
                return None
        return self._cache[f]


def output_latency(vcfg) -> dict:
    """OutputStream kwargs for [voice] output_latency_s (0 or less = PortAudio's default)."""
    lat = float(getattr(vcfg, "output_latency_s", 0) or 0)
    return {"latency": lat} if lat > 0 else {}


class Player:
    """Blocking playback in a worker thread, reporting loudness as it goes."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop_flag = threading.Event()
        self.sd = None
        if cfg.voice.enabled:
            try:
                import sounddevice as sd
                sd.query_devices(cfg.voice.device or None, "output")
                self.sd = sd
            except Exception as e:
                log.warning("no audio output (%s): Walter will mime", e)

    def play(self, audio: np.ndarray, sr: int, on_level: Callable[[float], None], silent: bool,
             pause: float = 0.12):
        block = sr // 30
        stream = None
        if self.sd and not silent:
            try:
                stream = self.sd.OutputStream(samplerate=sr, channels=1, dtype="int16",
                                              device=self.cfg.voice.device or None,
                                              **output_latency(self.cfg.voice))
                stream.start()
            except Exception as e:
                log.warning("audio output failed: %s", e)
                stream = None
        try:
            import time
            t0 = time.monotonic()
            for i in range(0, len(audio), block):
                if self.stop_flag.is_set():
                    break
                chunk = audio[i:i + block]
                rms = math.sqrt(float(np.mean(chunk.astype(np.float32) ** 2))) if len(chunk) else 0.0
                on_level(min(1.0, (rms / 5000.0) ** 0.8))
                if stream:
                    stream.write(chunk.reshape(-1, 1))
                else:
                    # keep real time when miming
                    target = t0 + (i + len(chunk)) / sr
                    delay = target - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
            # small natural pause between sentences
            if stream and pause > 0 and not self.stop_flag.is_set():
                stream.write(np.zeros(int(sr * pause), np.int16).reshape(-1, 1))
        finally:
            on_level(0.0)
            if stream:
                try:
                    # stopped mid-sentence: drop the buffered audio at once (a bigger
                    # output buffer must not delay a barge-in); else let it play out
                    (stream.abort if self.stop_flag.is_set() else stream.stop)()
                    stream.close()
                except Exception:
                    pass


class Stale(Exception):
    """A queued sentence was cancelled (stop() since it was queued): skip it."""


CAPTIONS_ONLY = "captions"   # _timed_synth's "cloud" flag: show the caption, play no sound


class Voice:
    """Ordered queue of things to say and face cues to fire at the right moment.

    One reply, one voice: the first sentence of a reply fixes its voice (cloud
    or piper) and the rest follows it, so a slow cloud sentence never flips the
    voice mid-reply. A reply is anything said with less than REPLY_GAP_S of
    silence in between (so an instant "One sec." and the answer count as one)."""

    REPLY_GAP_S = 20.0

    def __init__(self, cfg: Config, on_level: Callable[[float], None],
                 on_cue: Callable[[str, str], None], on_speaking: Callable[[bool], None]):
        self.cfg = cfg
        self.synth = Synth(cfg)
        self.player = Player(cfg)
        self.sfx = Sfx()
        self.on_level = on_level
        self.on_cue = on_cue
        self.on_speaking = on_speaking
        # on_caption(text, words, dur): karaoke caption cue; words = [[word, start, end], ...]
        # in seconds from now. Unset: a plain on_cue("caption", text).
        self.on_caption: Callable[[str, list, float], None] | None = None
        self.speech = None   # walter.speech.Speech (cloud voice switch), set by main
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.ready: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.busy = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.generation = 0  # bumped on stop() so stale items are dropped
        # what stop() threw away since start (counts only; /api/status cancelled_stale)
        self.stale_counts = {"queued": 0, "synth": 0, "cloud": 0}
        self.hushed = False  # mic muted by the person: captions only, no sound
        self._tasks: list[asyncio.Task] = []
        # One synthesis at a time, in order: on this 2-core CPU, sentence 2
        # synthesising alongside sentence 1 only delays the first audio.
        from concurrent.futures import ThreadPoolExecutor
        self._synth_pool = ThreadPoolExecutor(1, thread_name_prefix="voice-synth")
        # one-shot callbacks: fired (with that clip's synthesis time) when the
        # next spoken clip starts playing (latency.py)
        self._audio_marks: list[tuple[int, Callable[[float | None], None]]] = []
        # Short lines said often ("One sec."): synthesised once per voice/settings,
        # then played straight from memory, even when cloud speech is slow.
        self._cacheable: set[str] = set()
        self._clip_cache: dict[tuple, tuple] = {}
        # the voice this reply is in: None (not decided yet), "openai" or "local"
        self._reply_voice: str | None = None
        # the local engine this reply is in: None (not decided yet), "kokoro" or "piper"
        self._reply_local: str | None = None
        self._demo_kokoro = None   # Kokoro loaded just for the demo (engine is piper)
        self._reply_t = 0.0          # when the reply last said something (monotonic)
        self._pending_synth = 0      # synthesis jobs queued or running

    def start(self):
        self._tasks = [asyncio.create_task(self._prepare()), asyncio.create_task(self._play())]

    def put(self, kind: str, value: str | None = None, sound: bool = True):
        """Queue an event. A laugh action also queues a laugh sound right after
        the face cue (unless sound=False, e.g. in a [silent] reply)."""
        if kind == "action" and value and value.startswith("laugh"):
            self._put(kind, "laugh")  # the face knows one laugh animation
            if sound and getattr(self.cfg.voice, "laugh_sounds", True):
                self._put("sfx", value)
            return
        self._put(kind, value)

    def _put(self, kind: str, value: str | None, provider: str | None = None):
        self.busy += 1
        self.idle.clear()
        self.inbox.put_nowait((self.generation, kind, value, provider))

    def say(self, text: str, provider: str | None = None):
        """Queue a sentence. provider "local"/"openai" forces one voice for it
        (the A/B demo); None = whatever the switch says."""
        if provider:
            self._put("say", text, provider)
        else:
            self.put("say", text)

    def on_next_audio(self, cb: Callable[[float | None], None]):
        """Call cb(synth_seconds) when the next spoken clip starts playing."""
        self._audio_marks.append((self.generation, cb))

    def cache_lines(self, lines) -> None:
        """Keep these lines' audio in memory and synthesise them now, in the
        background (the synthesis thread, so it never competes with a reply)."""
        new = [t for t in lines if t not in self._cacheable]
        self._cacheable.update(new)
        if new:   # one at a time: a reply never waits behind more than one of these
            self._synth_pool.submit(self._warm_one, new)

    def _warm_one(self, todo: list[str]):
        text, rest = todo[0], todo[1:]
        try:
            if self._cached(text, None) is None:
                self._timed_synth(text, warm=True)
        except Exception as e:
            log.debug("couldn't pre-synthesise %r: %s", text, e)
        if rest:
            try:
                self._synth_pool.submit(self._warm_one, rest)
            except RuntimeError:   # shutting down
                pass

    def _clip_key(self, text: str, provider: str | None) -> tuple:
        sp = self.speech
        prov = provider or (sp.tts if sp is not None else "local")
        return (prov, text, repr(self.cfg.voice), repr(getattr(self.cfg, "speech", None)))

    def _cached(self, text: str, provider: str | None):
        if text not in self._cacheable:
            return None
        return self._clip_cache.get(self._clip_key(text, provider))

    def reply_voice(self) -> str | None:
        """The voice the current reply is in, or None once it has been quiet
        for REPLY_GAP_S (the next sentence starts a new reply)."""
        if self._reply_voice is not None and time.monotonic() - self._reply_t > self.REPLY_GAP_S:
            self._reply_voice = None
            self._reply_local = None
        return self._reply_voice

    def _set_reply_voice(self, which: str):
        if time.monotonic() - self._reply_t > self.REPLY_GAP_S:
            self._reply_local = None    # a new reply: its local engine is decided afresh
        if self._reply_voice != which:
            log.info("reply voice: %s", which)
        self._reply_voice = which
        self._reply_t = time.monotonic()

    def _cloud_with_retry(self, text: str, cancelled=None):
        """Cloud audio for one sentence: one more, quicker try if the first
        fails (a blip), but not when it can't work (cap reached, no key)."""
        got = self._cloud_synth(text, cancelled)
        if got is not None or (cancelled is not None and cancelled()):
            return got
        sp = self.speech
        retry_s = float(getattr(getattr(self.cfg, "speech", None), "tts_retry_timeout_s", 0) or 0)
        if retry_s <= 0:
            return None
        try:
            if hasattr(sp, "tts_room") and sp.tts_room(len(text)):
                return None
            if hasattr(sp, "has_key") and not sp.has_key():
                return None
        except Exception:
            pass
        log.info("tts openai: retrying once (%g s)", retry_s)
        return self._cloud_synth(text, cancelled, timeout=retry_s)

    def _timed_synth(self, text: str, provider: str | None = None, gen: int | None = None,
                     warm: bool = False):
        """(audio, sr, words, synth seconds, cloud?) for one sentence. Raises
        Stale if stop() was called since it was queued (gen): queued jobs of a
        stopped reply cost nothing, and a cancelled cloud call never falls back
        to piper. cloud? is CAPTIONS_ONLY when the cloud voice failed part-way
        through a reply: the sentence is captioned, not said in another voice.
        provider forces one voice (the A/B demo) and ignores the reply voice;
        warm = pre-synthesising a cached line (no retry, no reply voice)."""
        cancelled = (lambda: self.generation != gen) if gen is not None else None
        if cancelled is not None and cancelled():
            self.stale_counts["synth"] += 1
            raise Stale()
        t0 = time.monotonic()
        sp = self.speech
        follow = provider is None and not warm   # part of a reply: one voice throughout
        if sp is not None and (provider or sp.tts) == "openai":
            rv = self.reply_voice() if follow else None
            if rv == "local":   # the cloud failed earlier in this reply: stay with piper
                return self._local(text, "local", t0)
            hit = self._cached(text, provider) if follow else None
            if hit is not None:   # queued behind other synthesis, found in the cache meanwhile
                self._set_reply_voice("openai" if hit[3] else "local")
                return (*hit[:3], time.monotonic() - t0, hit[3])
            got = self._cloud_with_retry(text, cancelled) if follow else self._cloud_synth(text, cancelled)
            if got is not None:
                if text in self._cacheable:
                    self._clip_cache[self._clip_key(text, provider)] = (*got, True)
                if follow:
                    self._set_reply_voice("openai")
                return (*got, time.monotonic() - t0, True)
            if cancelled is not None and cancelled():
                raise Stale()
            if warm:
                raise RuntimeError("cloud voice unavailable")   # don't cache a piper ack
            if rv == "openai":
                # already speaking in the cloud voice: caption this one, don't switch voice
                log.warning("tts openai failed mid-reply; caption only for %d chars", len(text))
                self._set_reply_voice("openai")
                words, dur = reading_timings(text)
                return mime_audio(dur), 16000, words, time.monotonic() - t0, CAPTIONS_ONLY
            if follow:
                log.warning("tts openai failed before the reply began; the local voice says "
                            "the whole reply")
                self._set_reply_voice("local")
            return self._local(text, "local", t0, follow=follow, cache=False)   # a fallback
        if follow:
            self._set_reply_voice("local")
        return self._local(text, provider, t0, follow=follow)

    def _local(self, text: str, provider: str | None, t0: float, follow: bool = False,
               cache: bool = True):
        """Local voice for one sentence. Kokoro (if it's the engine) or piper, one
        engine per reply: if Kokoro fails before the reply has said anything in it,
        piper says the whole reply; if it fails part-way, that sentence is captioned."""
        engine, kokoro, kvoice = self._local_choice(provider, follow)
        audio = None
        if engine == "kokoro":
            try:
                audio, sr, words = self.synth.kokoro_timed(text, kokoro, kvoice)
                self.synth.kokoro_failures = 0
            except Exception as e:
                log.warning("tts kokoro failed (%s)", e)
                self._kokoro_failed()
                if follow and self._reply_local == "kokoro":
                    log.warning("kokoro failed mid-reply; caption only for %d chars", len(text))
                    words, dur = reading_timings(text)
                    return mime_audio(dur), 16000, words, time.monotonic() - t0, CAPTIONS_ONLY
                engine = "piper"
        if audio is None:
            audio, sr, words = self.synth.synth_timed(text)
        if follow:
            if self._reply_local != engine:
                log.info("reply local voice: %s", engine)
            self._reply_local = engine
        if cache and text in self._cacheable:
            self._clip_cache[self._clip_key(text, provider)] = (audio, sr, words, False)
        return audio, sr, words, time.monotonic() - t0, False

    def _local_choice(self, provider: str | None, follow: bool):
        """(engine, Kokoro object or None for the default, Kokoro voice name or None)."""
        if provider == "piper":
            return "piper", None, None
        if provider and provider.startswith("kokoro:"):   # the demo: a named Kokoro voice
            name = provider.split(":", 1)[1]
            k = self.synth.kokoro or self._demo_kokoro
            if k is None:
                k = self._demo_kokoro = self.synth.load_kokoro(name)
            return ("kokoro", k, name) if k is not None else ("piper", None, None)
        if follow and self._reply_local is not None:
            # (Kokoro unloaded mid-reply: kokoro_timed raises, so the sentence is captioned)
            return self._reply_local, None, None
        return self.synth.local_engine, None, None

    def _kokoro_failed(self):
        self.synth.kokoro_failures += 1
        if self.synth.kokoro_failures >= 3 and self.synth.kokoro is not None:
            log.warning("kokoro failed 3 times in a row; using piper until the voice is reloaded")
            self.synth.kokoro = None

    def _cloud_synth(self, text: str, cancelled=None, timeout: float | None = None):
        """OpenAI voice for one sentence, at piper's sample rate, with word
        timings estimated from each word's length (the API gives none)."""
        spoken = speech_text(text) or text
        piper = self.synth.piper
        sr = int(getattr(getattr(piper, "config", None), "sample_rate", 0) or 22050)
        kw = {}
        if cancelled is not None:
            kw["cancelled"] = cancelled
        if timeout is not None:
            kw["timeout"] = timeout
        try:
            audio = self.speech.synth(spoken, sr, **kw)
        except TypeError:   # a speech object without cancel/timeout support
            audio = self.speech.synth(spoken, sr)
        if audio is None or not len(audio):
            return None
        words = text.split()
        units = [self.synth._word_units(w)[0] for w in words]
        return audio, sr, layout_words(words, units, *speech_bounds(audio, sr))

    def _synth_job(self, loop, text: str, provider: str | None = None, gen: int | None = None):
        self._pending_synth += 1
        fut = loop.run_in_executor(self._synth_pool, self._timed_synth, text, provider, gen)
        fut.add_done_callback(self._synth_done)
        return fut

    def _synth_done(self, _fut):
        self._pending_synth = max(0, self._pending_synth - 1)

    def _fire_audio_marks(self, synth_s: float | None):
        marks, self._audio_marks = self._audio_marks, []
        for gen, cb in marks:
            if gen == self.generation:
                try:
                    cb(synth_s)
                except Exception as e:  # timing must never break speech
                    log.debug("audio mark failed: %s", e)

    async def wait_idle(self):
        await self.idle.wait()

    def stop(self):
        """Shut up immediately (barge-in / 'stop' / mute / sleep). Everything
        queued before now is stale: sentences not yet synthesised are dropped,
        queued synthesis jobs become no-ops, and a cloud synthesis in flight is
        cut off (it would otherwise keep costing money and block the next one)."""
        self.generation += 1
        self.player.stop_flag.set()
        dropped = 0
        while True:   # not yet prepared: drop them now (the busy count goes with them)
            try:
                self.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
            self._done_one()
        cut = 0
        sp = self.speech
        if sp is not None and hasattr(sp, "abort"):
            try:
                cut = sp.abort("tts")
            except Exception as e:
                log.debug("tts abort failed: %s", e)
        self.stale_counts["queued"] += dropped
        self.stale_counts["cloud"] += cut
        if dropped or cut:
            log.info("voice stop: dropped %d queued item(s), cut %d cloud synthesis", dropped, cut)

    def stale_total(self) -> int:
        return sum(self.stale_counts.values())

    def _done_one(self):
        self.busy -= 1
        if self.busy <= 0:
            self.busy = 0
            self.idle.set()

    async def _prepare(self):
        loop = asyncio.get_running_loop()
        while True:
            gen, kind, value, provider = await self.inbox.get()
            fut = None
            if kind == "say" and gen == self.generation and not self.hushed:
                # instant from the cache only if nothing is synthesising ahead of it (that
                # sentence may still switch the reply's voice) and it's in the reply's voice
                hit = None
                if not self._pending_synth:
                    hit = self._cached(value, provider)
                    if hit is not None and provider is None:
                        rv = self.reply_voice()
                        if rv is not None and rv != ("openai" if hit[3] else "local"):
                            hit = None
                        else:
                            self._set_reply_voice("openai" if hit[3] else "local")
                if hit is not None:   # instant: no synthesis queue at all
                    fut = loop.create_future()
                    fut.set_result((hit[0], hit[1], hit[2], 0.0, hit[3]))
                else:
                    fut = self._synth_job(loop, value, provider, gen)
            await self.ready.put((gen, kind, value, fut, provider))

    async def _play(self):
        loop = asyncio.get_running_loop()
        speaking = False
        while True:
            gen, kind, value, fut, provider = await self.ready.get()
            try:
                if gen != self.generation:
                    if kind == "say":
                        self.stale_counts["queued"] += 1   # prefetched, never played
                    if fut is not None:   # a stale job finishes on its own; silence its result
                        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
                    continue
                if kind in ("emotion", "action"):
                    self.on_cue(kind, value)
                    continue
                if kind == "sfx":
                    if self.hushed:
                        continue  # muted by the person: no sound at all
                    clip = self.sfx.pick(value)
                    if clip is None:
                        continue
                    if not speaking:
                        speaking = True
                        self.on_speaking(True)
                    self.player.stop_flag.clear()
                    lvl = lambda v: loop.call_soon_threadsafe(self.on_level, v)
                    await loop.run_in_executor(None, self.player.play, clip[0], clip[1], lvl,
                                               self.synth.kind == "mime", 0.06)
                    continue
                if kind == "say":
                    synth_s, cloud = None, False
                    if self.hushed:
                        # muted: captions only, paced for reading, no sound
                        words, dur = reading_timings(value)
                        audio, sr = mime_audio(dur), 16000
                    else:
                        try:
                            if fut is None:  # unmuted since it was queued
                                fut = self._synth_job(loop, value, provider, gen)
                            audio, sr, words, synth_s, cloud = await fut
                        except Stale:
                            continue
                        except Exception as e:
                            log.error("tts failed for %r: %s", value, e)
                            continue
                        dur = len(audio) / sr if sr else 0.0
                    if gen != self.generation:
                        continue
                    if not speaking:
                        speaking = True
                        self.on_speaking(True)
                    if self._audio_marks:
                        self._fire_audio_marks(synth_s)
                    if self.on_caption:
                        self.on_caption(value, words, round(dur, 3))
                    else:
                        self.on_cue("caption", value)
                    self.player.stop_flag.clear()
                    lvl = lambda v: loop.call_soon_threadsafe(self.on_level, v)
                    silent = ((self.synth.kind == "mime" and not cloud) or self.hushed
                              or cloud == CAPTIONS_ONLY)
                    await loop.run_in_executor(None, self.player.play, audio, sr, lvl, silent)
                    if self._reply_voice is not None:
                        self._reply_t = time.monotonic()   # the reply's gap counts from here
            finally:
                self._done_one()
                if self.busy == 0 and speaking:
                    speaking = False
                    self.on_speaking(False)
