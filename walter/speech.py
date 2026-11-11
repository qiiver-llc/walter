"""Switchable speech providers: local (piper / faster-whisper) or OpenAI.

Two independent switches, persisted in ~/.walter/speech.json:
  tts ("voice"): "local" = piper (voice.py, unchanged) | "openai" = OpenAI text-to-speech
  stt ("ears"):  "local" = faster-whisper (ears.py, unchanged) | "openai" = OpenAI transcription

Privacy and safety rules this module keeps:
- The API key is read from ~/.walter/secrets/openai.key (or $OPENAI_API_KEY)
  at the moment of each call and is never logged, printed or returned.
- Only counts are logged (characters, seconds, latency, HTTP status): never
  the audio or the words.
- Live partial captions, wake-word gating and speaker recognition stay
  local; only a finished utterance that is meant for Walter is sent
  (see Ears._cloud_final), and only sentences Walter actually speaks.
- Daily and monthly caps (characters / seconds) are counted in
  ~/.walter/speech-usage.json; once reached, everything falls back to local.
- Any error or timeout falls back to local for that utterance; for speech, voice.py
  retries once and keeps each reply in one voice (whole reply in piper, or caption only).

All HTTP is plain http.client (sync): TTS runs in the voice's synthesis
thread, STT in the ears' thread. Calls in flight can be cancelled from any
thread with Speech.abort("tts"|"stt") (barge-in, stop, mute, sleep): the
socket is shut down, the call returns at once and is counted by what had
actually been received, not as a fallback.
"""
from __future__ import annotations

import datetime as dt
import http.client
import io
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .config import Config, DATA_DIR

log = logging.getLogger("walter.speech")

PROVIDERS = ("local", "openai")
MODES = ("classic", "realtime")   # conversation mode (realtime.py)
OPENAI_PCM_SR = 24000      # response_format "pcm": 24 kHz, 16-bit signed little-endian, mono
STT_SR = 16000             # what the ears record

# Approximate published prices (USD), November 2026. Only used for the
# "about $x" estimate in `walterctl speech status`; labelled approximate.
TTS_PRICE_PER_CHAR = {"tts-1": 15e-6, "tts-1-hd": 30e-6}
TTS_PRICE_PER_MIN = {"gpt-4o-mini-tts": 0.015}      # billed by audio, roughly per minute
STT_PRICE_PER_MIN = {"whisper-1": 0.006, "gpt-4o-transcribe": 0.006,
                     "gpt-4o-mini-transcribe": 0.003}
DEFAULT_TTS_PER_CHAR = 15e-6
DEFAULT_STT_PER_MIN = 0.006
# Realtime models, USD per 1M tokens (gpt-realtime-2.1-mini, Nov 2026 model page):
# (text in, cached text in, audio in, cached audio in, text out, audio out)
RT_PRICE_PER_M = {"gpt-realtime-2.1-mini": (0.6, 0.06, 10.0, 0.3, 2.4, 20.0)}
DEFAULT_RT_PRICE_PER_M = (0.6, 0.06, 10.0, 0.3, 2.4, 20.0)
RT_TOKEN_KEYS = ("rt_text_in", "rt_text_in_cached", "rt_audio_in", "rt_audio_in_cached",
                 "rt_text_out", "rt_audio_out")



def time_stretch(x: "np.ndarray", tempo: float) -> "np.ndarray":
    """Speed speech up (tempo > 1) or down without changing pitch (WSOLA via audiotsm).
    Returns x unchanged if tempo is ~1 or audiotsm isn't installed."""
    if not tempo or abs(tempo - 1.0) < 0.02 or len(x) < 2048:
        return x
    try:
        from audiotsm import wsola
        from audiotsm.io.array import ArrayReader, ArrayWriter
    except Exception:  # pragma: no cover - optional dependency
        return x
    reader = ArrayReader(x.reshape(1, -1).astype(np.float32))
    writer = ArrayWriter(channels=1)
    wsola(channels=1, speed=float(tempo)).run(reader, writer)
    return writer.data[0].astype(np.float32)


def echoes_prompt(text: str, prompt: str) -> bool:
    """True if a transcript is (mostly) the hint prompt we sent, not speech."""
    import re
    words = lambda t: re.findall(r"[a-z0-9']+", t.lower())
    t, p = words(text), set(words(prompt))
    if not t:
        return False
    if "###" in text:
        return True
    return len(t) >= 4 and sum(1 for w in t if w in p) / len(t) >= 0.8

class SpeechError(Exception):
    """A cloud call failed; the caller falls back to local. The message is
    safe to log (no key, no content)."""


class SpeechAborted(SpeechError):
    """The call was cancelled on purpose (stale: barge-in, stop, mute, sleep).
    The caller must NOT fall back to local: nobody wants the result any more."""


def _today() -> str:
    return dt.date.today().isoformat()


def wav_bytes(pcm: bytes, sr: int = STT_SR) -> bytes:
    """16-bit mono PCM -> a small in-memory wav file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Encode a multipart/form-data body. files: name -> (filename, data, mime)."""
    boundary = "walter-" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode())
        out.write(str(v).encode() + b"\r\n")
    for k, (fname, data, mime) in files.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                  f"filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n".encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class Speech:
    """Which speech providers are on, usage counting and the OpenAI calls."""

    def __init__(self, cfg: Config, data_dir: Path | str | None = None):
        self.cfg = cfg
        d = Path(data_dir) if data_dir else DATA_DIR
        self.state_file = d / "speech.json"
        self.usage_file = d / "speech-usage.json"
        self.key_file = Path(cfg.speech.key_file).expanduser() if cfg.speech.key_file \
            else d / "secrets" / "openai.key"
        self._lock = threading.RLock()
        self.on_change: Callable[[], None] | None = None   # cloud indicator may have changed
        s = cfg.speech
        self.tts = s.tts_provider if s.tts_provider in PROVIDERS else "local"
        self.stt = s.stt_provider if s.stt_provider in PROVIDERS else "local"
        self.mode = s.mode if s.mode in MODES else "classic"
        self.on_mode: Callable[[str], None] | None = None  # realtime.py: mode switched live
        self.rt_live = False      # a realtime session is streaming the mic right now
        self.rt_problem = ""      # why realtime isn't usable right now ("" = fine)
        self._load_state()
        self.usage: dict = self._load_usage()
        # counts since start (also in status): calls, fallbacks
        self.stats = {"tts_ok": 0, "tts_fallback": 0, "stt_ok": 0, "stt_fallback": 0,
                      "tts_aborted": 0, "stt_aborted": 0}
        # Open cloud connections per kind ("tts"/"stt"), so abort() can cut them
        # off from another thread; and an abort generation per kind, so a call
        # that is still connecting when abort() runs notices it was cancelled.
        self._live: dict[str, set] = {"tts": set(), "stt": set()}
        self._abort_gen = {"tts": 0, "stt": 0}
        self.last_error: dict[str, str] = {}

    # ------------------------------------------------------------ switches
    def _load_state(self):
        try:
            d = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        if d.get("tts") in PROVIDERS:
            self.tts = d["tts"]
        if d.get("stt") in PROVIDERS:
            self.stt = d["stt"]
        if d.get("mode") in MODES:
            self.mode = d["mode"]

    def _save_state(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"tts": self.tts, "stt": self.stt,
                                                   "mode": self.mode}))
        except OSError as e:
            log.warning("couldn't save speech providers: %s", e)

    def set(self, which: str, provider: str):
        """which: "tts" | "stt" (also "voice" | "ears"); provider: local | openai.
        Or which "mode", provider classic | realtime."""
        which = {"voice": "tts", "ears": "stt"}.get(which, which)
        if which == "mode":
            return self.set_mode(provider)
        if which not in ("tts", "stt"):
            raise ValueError("which must be tts/voice or stt/ears")
        if provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
        with self._lock:
            setattr(self, which, provider)
            self._save_state()
        log.info("speech: %s provider -> %s", which, provider)
        self.changed()

    def set_mode(self, mode: str):
        """Conversation mode: classic (brain + voice) or realtime (realtime.py)."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        with self._lock:
            self.mode = mode
            self._save_state()
        log.info("speech: conversation mode -> %s", mode)
        if self.on_mode:
            try:
                self.on_mode(mode)
            except Exception as e:
                log.warning("mode switch hook failed: %s", e)
        self.changed()

    def changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as e:
                log.debug("speech on_change failed: %s", e)

    # ------------------------------------------------------------ key
    def _key(self) -> str:
        """The API key, or "". Read fresh each time; never logged."""
        try:
            k = self.key_file.read_text().strip()
            if k:
                return k
        except OSError:
            pass
        return os.environ.get("OPENAI_API_KEY", "").strip()

    def has_key(self) -> bool:
        return bool(self._key())

    # ------------------------------------------------------------ usage
    def _load_usage(self) -> dict:
        try:
            d = json.loads(self.usage_file.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_usage(self):
        # keep about two months of days
        days = sorted(k for k in self.usage if len(k) == 10)
        for k in days[:-62]:
            self.usage.pop(k, None)
        try:
            self.usage_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.usage_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.usage, indent=1))
            tmp.replace(self.usage_file)
        except OSError as e:
            log.warning("couldn't save speech usage: %s", e)

    def _day(self, day: str | None = None) -> dict:
        return self.usage.get(day or _today(), {})

    def _month_total(self, key: str) -> float:
        month = _today()[:7]
        return sum(v.get(key, 0) for k, v in self.usage.items() if k.startswith(month))

    def _add(self, **counts):
        with self._lock:
            day = self.usage.setdefault(_today(), {})
            for k, v in counts.items():
                day[k] = round(day.get(k, 0) + v, 3)
            self._save_usage()

    def tts_room(self, chars: int) -> str:
        """"" if `chars` more characters fit the caps, else why not."""
        s = self.cfg.speech
        with self._lock:
            today = self._day().get("tts_chars", 0)
            if s.max_tts_chars_per_day > 0 and today + chars > s.max_tts_chars_per_day:
                return f"daily cap of {s.max_tts_chars_per_day} characters reached"
            if s.max_tts_chars_per_month > 0 and \
                    self._month_total("tts_chars") + chars > s.max_tts_chars_per_month:
                return f"monthly cap of {s.max_tts_chars_per_month} characters reached"
        return ""

    def stt_room(self, seconds: float) -> str:
        s = self.cfg.speech
        with self._lock:
            today = self._day().get("stt_seconds", 0)
            if s.max_stt_seconds_per_day > 0 and today + seconds > s.max_stt_seconds_per_day:
                return f"daily cap of {s.max_stt_seconds_per_day:g} seconds reached"
            if s.max_stt_seconds_per_month > 0 and \
                    self._month_total("stt_seconds") + seconds > s.max_stt_seconds_per_month:
                return f"monthly cap of {s.max_stt_seconds_per_month:g} seconds reached"
        return ""

    def rt_room(self, seconds: float = 1.0) -> str:
        """"" if `seconds` more of realtime session fit the caps, else why not."""
        s = self.cfg.speech
        with self._lock:
            today = self._day().get("rt_seconds", 0)
            if s.max_realtime_min_per_day > 0 and today + seconds > s.max_realtime_min_per_day * 60:
                return f"daily cap of {s.max_realtime_min_per_day:g} realtime minutes reached"
            if s.max_realtime_min_per_month > 0 and \
                    self._month_total("rt_seconds") + seconds > s.max_realtime_min_per_month * 60:
                return f"monthly cap of {s.max_realtime_min_per_month:g} realtime minutes reached"
        return ""

    def add_rt(self, seconds: float = 0.0, **tokens):
        """Count realtime use: seconds of open session, and tokens from response.done."""
        counts = {k: v for k, v in tokens.items() if v}
        if seconds > 0:
            counts["rt_seconds"] = seconds
        if counts:
            self._add(**counts)

    def rt_cost(self, day: dict) -> float:
        p = RT_PRICE_PER_M.get(self.cfg.speech.realtime_model, DEFAULT_RT_PRICE_PER_M)
        return sum(day.get(k, 0) * price / 1e6 for k, price in zip(RT_TOKEN_KEYS, p))

    def cost(self, day: dict) -> float:
        """Approximate dollars for one day's (or a summed) usage record."""
        s = self.cfg.speech
        if s.tts_model in TTS_PRICE_PER_MIN:
            tts = day.get("tts_audio_s", 0) / 60 * TTS_PRICE_PER_MIN[s.tts_model]
        else:
            tts = day.get("tts_chars", 0) * TTS_PRICE_PER_CHAR.get(s.tts_model, DEFAULT_TTS_PER_CHAR)
        stt = day.get("stt_seconds", 0) / 60 * STT_PRICE_PER_MIN.get(s.stt_model, DEFAULT_STT_PER_MIN)
        return tts + stt + self.rt_cost(day)

    # ------------------------------------------------------------ status
    def cloud_active(self) -> dict:
        """What's actually going to the cloud right now (for the face indicator)."""
        key = self.has_key()
        return {"tts": self.tts == "openai" and key and not self.tts_room(1),
                "stt": self.stt == "openai" and key and not self.stt_room(0.1)}

    def indicator(self) -> dict:
        a = self.cloud_active()
        return {"t": "cloud", "tts": bool(a["tts"]), "stt": bool(a["stt"]),
                "rt": bool(self.rt_live),
                "idle_rt": self.mode == "realtime" and not self.rt_live,
                # chosen but not usable right now (no key yet / cap reached): shown dimmed
                "idle_tts": self.tts == "openai" and not a["tts"],
                "idle_stt": self.stt == "openai" and not a["stt"]}

    def status(self) -> dict:
        s = self.cfg.speech
        with self._lock:
            today = dict(self._day())
            month = {k: self._month_total(k) for k in ("tts_chars", "tts_audio_s", "stt_seconds",
                                                       "tts_calls", "stt_calls", "rt_seconds",
                                                       *RT_TOKEN_KEYS)}
        return {
            "tts": self.tts, "stt": self.stt, "mode": self.mode,
            "realtime": {"model": s.realtime_model, "voice": s.realtime_voice,
                         "live": self.rt_live, "room": self.rt_room(1) or "ok",
                         "problem": self.rt_problem,
                         "minutes_today": round(today.get("rt_seconds", 0) / 60, 2),
                         "minutes_month": round(month["rt_seconds"] / 60, 2),
                         "max_min_per_day": s.max_realtime_min_per_day,
                         "max_min_per_month": s.max_realtime_min_per_month,
                         "approx_cost_today_usd": round(self.rt_cost(today), 4),
                         "approx_cost_month_usd": round(self.rt_cost(month), 4)},
            "key": self.has_key(),          # present or not; never the key itself
            "active": self.cloud_active(),
            "tts_model": s.tts_model, "tts_voice": s.tts_voice, "stt_model": s.stt_model,
            "today": today, "month": month,
            "approx_cost_today_usd": round(self.cost(today), 4),
            "approx_cost_month_usd": round(self.cost(month), 4),
            "caps": {"max_tts_chars_per_day": s.max_tts_chars_per_day,
                     "max_stt_seconds_per_day": s.max_stt_seconds_per_day,
                     "max_tts_chars_per_month": s.max_tts_chars_per_month,
                     "max_stt_seconds_per_month": s.max_stt_seconds_per_month},
            "tts_room": self.tts_room(1) or "ok", "stt_room": self.stt_room(0.1) or "ok",
            "since_start": dict(self.stats),
            "last_error": dict(self.last_error),
        }

    # ------------------------------------------------------------ http
    def _request(self, path: str, body: bytes, ctype: str, timeout: float, kind: str = "",
                 gen: int | None = None):
        """POST to the API. Returns (connection, response). The connection is
        registered under `kind` while open so abort(kind) can close it."""
        key = self._key()
        if not key:
            raise SpeechError("no API key")
        u = urllib.parse.urlsplit(self.cfg.speech.openai_base_url.rstrip("/") + path)
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        target = u.path + (f"?{u.query}" if u.query else "")
        live = self._live.setdefault(kind, set()) if kind else None
        if live is not None:
            with self._lock:
                if gen is not None and gen != self._abort_gen.get(kind, 0):
                    raise SpeechAborted("cancelled")
                live.add(conn)
        sent = False
        try:
            conn.request("POST", target, body=body,
                         headers={"Authorization": "Bearer " + key, "Content-Type": ctype})
            sent = True
            resp = conn.getresponse()
        except Exception as e:
            self._release(kind, conn)
            if kind and gen is not None and gen != self._abort_gen.get(kind, 0):
                err = SpeechAborted("cancelled")
                err.sent = sent   # the request (e.g. the audio upload) had gone out
                raise err from None
            if isinstance(e, (OSError, http.client.HTTPException)):
                raise SpeechError(f"network: {type(e).__name__}") from None
            raise
        if resp.status >= 400:
            self._release(kind, conn)
            raise SpeechError(f"HTTP {resp.status}")
        return conn, resp

    def _release(self, kind: str, conn):
        if kind:
            with self._lock:
                self._live.get(kind, set()).discard(conn)
        try:
            conn.close()
        except Exception:
            pass

    def abort(self, kind: str | None = None) -> int:
        """Cancel in-flight cloud calls of `kind` ("tts", "stt" or None = both):
        their sockets are shut down so the blocked read returns at once, and the
        call reports SpeechAborted (no local fallback). Safe from any thread.
        Returns how many connections were cut."""
        n = 0
        for k in ([kind] if kind else list(self._live)):
            with self._lock:
                self._abort_gen[k] = self._abort_gen.get(k, 0) + 1
                conns = list(self._live.get(k, ()))
            for conn in conns:
                n += 1
                sock = getattr(conn, "sock", None)
                try:
                    if sock is not None:
                        sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if n:
            log.info("speech: aborted %d in-flight cloud request(s) (%s) as stale", n, kind or "all")
        return n

    def _gen(self, kind: str) -> int:
        with self._lock:
            return self._abort_gen.get(kind, 0)

    def _fail(self, which: str, why: str):
        self.stats[which + "_fallback"] += 1
        self.last_error[which] = f"{time.strftime('%H:%M:%S')} {why}"

    # ------------------------------------------------------------ TTS
    def synth(self, text: str, out_sr: int = 22050,
              cancelled: Callable[[], bool] | None = None,
              timeout: float | None = None) -> np.ndarray | None:
        """OpenAI text-to-speech for one sentence -> int16 samples at out_sr,
        or None (caller uses piper): capped, no key, error or slower than
        tts_timeout_s. Also None if it was cancelled (abort("tts") or
        `cancelled()` turning true): check that before falling back.
        `timeout` overrides tts_timeout_s (the quick retry). Logs counts only."""
        s = self.cfg.speech
        tmo = s.tts_timeout_s if timeout is None else timeout
        chars = len(text)
        if not text.strip():
            return None
        room = self.tts_room(chars)
        if room:
            self._fail("tts", room)
            log.info("tts openai skipped (%s); local voice", room)
            self.changed()
            return None
        payload = {"model": s.tts_model, "voice": s.tts_voice, "input": text,
                   "response_format": "pcm"}
        if s.tts_model.startswith("gpt-4o"):
            if s.tts_instructions:
                payload["instructions"] = s.tts_instructions
        else:
            # tts-1 / tts-1-hd honour speed. To lower the pitch (tts_pitch < 1) we ask for
            # proportionally faster speech and then play it back slower (resample below),
            # so the final tempo stays at tts_speed.
            pitch = s.tts_pitch if 0.7 <= s.tts_pitch <= 1.3 else 1.0
            sp = (s.tts_speed or 1.0) / pitch
            if abs(sp - 1.0) > 1e-3:
                payload["speed"] = max(0.25, min(4.0, sp))
        t0 = time.monotonic()
        deadline = t0 + max(0.5, tmo)
        gen = self._gen("tts")
        buf = bytearray()
        conn = None

        def aborted() -> bool:
            return self._gen("tts") != gen or bool(cancelled and cancelled())

        try:
            if aborted():
                raise SpeechAborted("cancelled")
            conn, resp = self._request("/audio/speech", json.dumps(payload).encode(),
                                       "application/json", tmo, "tts", gen)
            while True:   # streamed body: read as it arrives, give up past the deadline
                if aborted():
                    raise SpeechAborted("cancelled")
                if time.monotonic() > deadline:
                    raise SpeechError(f"timeout after {tmo:g} s")
                try:
                    piece = resp.read1(16384)
                except Exception:
                    if aborted():
                        raise SpeechAborted("cancelled") from None
                    raise
                if not piece:
                    if aborted():   # the socket was shut down under us: not a real end
                        raise SpeechAborted("cancelled")
                    break
                buf += piece
            if len(buf) < 2:
                raise SpeechError("empty audio")
        except SpeechAborted:
            self._aborted_tts(chars, len(buf), time.monotonic() - t0)
            return None
        except SpeechError as e:
            self._fail("tts", str(e))
            log.warning("tts openai failed (%s); voice.py decides the fallback", e)
            return None
        except Exception as e:   # socket timeouts mid-read etc.
            self._fail("tts", type(e).__name__)
            log.warning("tts openai failed (%s); voice.py decides the fallback", type(e).__name__)
            return None
        finally:
            if conn is not None:
                self._release("tts", conn)
        took = time.monotonic() - t0
        pcm = np.frombuffer(bytes(buf[:len(buf) // 2 * 2]), "<i2")
        audio_s = len(pcm) / OPENAI_PCM_SR
        self._add(tts_chars=chars, tts_audio_s=audio_s, tts_calls=1)
        self.stats["tts_ok"] += 1
        log.info("tts openai: %d chars -> %.1f s audio in %.2f s", chars, audio_s, took)
        x = pcm.astype(np.float32) / 32768.0
        x = time_stretch(x, s.tts_tempo)  # the API ignores 'speed' for gpt-4o voices
        pitch = s.tts_pitch if (0.7 <= s.tts_pitch <= 1.3 and not s.tts_model.startswith("gpt-4o")) else 1.0
        src_sr = int(round(OPENAI_PCM_SR * pitch))  # pitch < 1: samples are played back slower = deeper
        if out_sr != src_sr:
            from .voice import resample
            x = resample(x, src_sr, out_sr)
        vol = self.cfg.voice.volume if self.cfg.voice.volume > 0 else 1.0
        return (np.clip(x * vol, -1.0, 1.0) * 32767).astype(np.int16)

    def _aborted_tts(self, chars: int, got_bytes: int, took: float):
        """Count a cancelled TTS call by what actually arrived: the audio seconds
        received, and its characters only if audio had started (by then the
        request was accepted and is billed by its input); nothing if it was cut
        off before any audio. Not a fallback, not an error."""
        self.stats["tts_aborted"] += 1
        audio_s = got_bytes // 2 / OPENAI_PCM_SR
        counts = {"tts_aborted": 1}
        if got_bytes >= 2:
            counts.update(tts_chars=chars, tts_audio_s=audio_s, tts_calls=1)
        self._add(**counts)
        log.info("tts openai: cancelled as stale after %.2f s (%.1f s audio received)", took, audio_s)

    # ------------------------------------------------------------ STT
    def transcribe(self, pcm: bytes, prompt: str = "") -> str | None:
        """OpenAI transcription of one finished utterance (16 kHz int16 mono).
        Returns the text ("" = it heard nothing), or None on cap/error (the
        caller keeps the local whisper result). Logs counts only."""
        s = self.cfg.speech
        secs = len(pcm) / (STT_SR * 2)
        room = self.stt_room(secs)
        if room:
            self._fail("stt", room)
            log.info("stt openai skipped (%s); local whisper", room)
            self.changed()
            return None
        fields = {"model": s.stt_model, "language": "en", "response_format": "json"}
        if prompt:
            fields["prompt"] = prompt
        body, ctype = multipart(fields, {"file": ("utterance.wav", wav_bytes(pcm), "audio/wav")})
        t0 = time.monotonic()
        gen = self._gen("stt")
        conn = None
        try:
            conn, resp = self._request("/audio/transcriptions", body, ctype, s.stt_timeout_s, "stt", gen)
            try:
                raw = resp.read()
            except Exception:
                if self._gen("stt") != gen:
                    raise SpeechAborted("cancelled") from None
                raise
            if self._gen("stt") != gen:
                raise SpeechAborted("cancelled")
            d = json.loads(raw or b"{}")
            text = d.get("text")
            if not isinstance(text, str):
                raise SpeechError("bad response")
        except SpeechAborted as e:
            # The upload had finished (the request was sent): billed as sent, so
            # counted as used; aborted before then: nothing counted.
            self.stats["stt_aborted"] += 1
            sent = conn is not None or getattr(e, "sent", False)
            self._add(stt_aborted=1, **({"stt_seconds": secs, "stt_calls": 1} if sent else {}))
            log.info("stt openai: cancelled as stale after %.2f s", time.monotonic() - t0)
            return None
        except SpeechError as e:
            self._fail("stt", str(e))
            log.warning("stt openai failed (%s); local whisper for this utterance", e)
            return None
        except Exception as e:
            self._fail("stt", type(e).__name__)
            log.warning("stt openai failed (%s); local whisper for this utterance", type(e).__name__)
            return None
        finally:
            if conn is not None:
                self._release("stt", conn)
        self._add(stt_seconds=secs, stt_calls=1)
        self.stats["stt_ok"] += 1
        log.info("stt openai: %.1f s audio in %.2f s", secs, time.monotonic() - t0)
        text = text.strip()
        if prompt and echoes_prompt(text, prompt):
            # On silence/noise the model sometimes just repeats the prompt back.
            log.info("stt openai: result was the prompt echoed back; treating as no speech")
            return ""
        return text
