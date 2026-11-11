"""Diagnostics bundle for beta feedback: technical facts only, redacted, never sent by itself.

    walterctl diagnostics [--out FILE] [--json]

What goes in (and nothing else):
  version     app VERSION, git commit, release name (if release-managed)
  system      OS, kernel, Python, CPU model/threads, load, RAM, disk free, uptime
  audio       sound card / PipeWire device names (redacted)
  service     how long the walter service has been up
  config      ONLY keys on the ALLOW list below (no paths, names, places, URLs, keys);
              every value also goes through the redactor
  tasks       worker states: counts plus id/status/minutes (never titles or instructions)
  log         last 200 log lines, redacted
  errors      recent error/traceback lines, redacted

The redactor removes emails, IP and MAC addresses, URLs/hostnames, tokens and keys,
long hex/base64 runs, phone numbers, paths under ~/.walter/secrets, the home folder
name, and every word in ~/.walter/private-words.txt (names, town...).

Building a bundle sends nothing anywhere. walter/feedback.py only POSTs it when the
owner taps "Send to the Walter team" after seeing the preview.
"""
from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOG_LINES = 200
ERROR_LINES = 30
MAX_LINE = 400
SCHEMA = 1

# ------------------------------------------------------------------ config allow-list
# Section -> keys whose values are technical and non-personal. Anything not listed is
# left out entirely (only the count of omitted keys is reported).
_NUM_BOOL = object()   # marker: numbers/bools only
ALLOW: dict[str, set[str]] = {
    "server": {"port", "setup_port"},
    "brain": {"model", "fast_model", "deep_model", "auto_switch", "permission_mode", "turn_timeout",
              "max_tasks", "task_timeout", "task_model", "worker_cpu_weight", "worker_io_weight",
              "worker_nice"},
    "ears": {"enabled", "device", "whisper_model", "whisper_compute", "whisper_threads", "whisper_nice",
             "final_model", "vad_aggressiveness", "end_silence_ms", "adaptive_end_of_turn",
             "end_silence_complete_ms", "end_silence_unfinished_ms", "max_utterance_s", "chunk_s",
             "noise_gate_ratio", "min_speech_ms", "silero_vad", "silero_threshold", "silero_rescue",
             "silero_rescue_min_s", "silero_rescue_ratio", "silero_rescue_loud_s", "silero_rescue_no_speech",
             "max_no_speech_prob", "min_avg_logprob", "max_compression_ratio", "live_partials",
             "partial_interval_s", "partial_max_s", "early_final", "beam_size", "require_wake_word",
             "followup_window_s", "startup_window_s", "barge_in", "barge_in_min_ms", "barge_in_window_s",
             "barge_in_energy_ratio", "barge_in_recheck_s", "aec_device"},
    "voice": {"enabled", "device", "aec_device", "length_scale", "volume", "pitch", "output_latency_s",
              "laugh_sounds", "engine", "kokoro_voice", "kokoro_speed", "kokoro_threads"},
    "speech": {"tts_provider", "stt_provider", "tts_model", "tts_voice", "tts_speed", "tts_pitch", "tts_tempo",
               "tts_timeout_s", "tts_retry_timeout_s", "stt_model", "stt_timeout_s", "max_tts_chars_per_day",
               "max_stt_seconds_per_day", "max_tts_chars_per_month", "max_stt_seconds_per_month", "mode",
               "realtime_model", "realtime_voice", "realtime_speed", "realtime_vad", "realtime_vad_threshold",
               "realtime_silence_ms", "realtime_input", "realtime_max_output_tokens", "realtime_noise_reduction",
               "realtime_idle_close_s", "realtime_tool_timeout_s", "realtime_fallback_min",
               "max_realtime_min_per_day", "max_realtime_min_per_month"},
    "life": {"heartbeat_minutes", "quiet_hours", "sleep_after_idle_min", "muted_sleep_min", "captions"},
    "senses": {"camera", "width", "height", "fps", "arrive_after_min", "empty_after_min", "greet_cooldown_min",
               "debug", "camera_sleep", "sleep_after_min", "verify_s", "wake_ratio", "wake_cooldown_s",
               "recognise_faces", "recognise_voices", "face_threshold", "voice_threshold"},
    # lat/lon, list title, teams, keywords: personal -> not listed
    "dashboard": {"enabled", "dark_hours", "phone_page", "sports_rows", "verse_line"},
    "app": {"enabled", "allow_lan", "max_talk_s"},          # public_url is personal
    "feedback": set(),                                      # the endpoint URL is not needed
}
SAFE_STR = re.compile(r"^[A-Za-z0-9 ._:+-]{0,80}$")


def _safe_value(v, red: "Redactor"):
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        v = red(v)
        return v if SAFE_STR.match(v) else "[redacted]"
    if isinstance(v, list) and len(v) <= 8 and all(isinstance(x, (int, float, str)) for x in v):
        return [_safe_value(x, red) for x in v]
    return "[redacted]"


def redacted_config(raw: dict, red: "Redactor") -> dict:
    """Only allow-listed keys of known sections; everything else dropped and counted."""
    out: dict = {}
    omitted = 0
    for section, values in (raw or {}).items():
        allowed = ALLOW.get(section)
        if not isinstance(values, dict) or allowed is None:
            omitted += len(values) if isinstance(values, dict) else 1
            continue
        for k, v in values.items():
            if k in allowed:
                out.setdefault(section, {})[k] = _safe_value(v, red)
            else:
                omitted += 1
    out["_omitted_keys"] = omitted
    return out


# ------------------------------------------------------------------ redactor
class Redactor:
    """Callable: text -> text with personal and secret bits replaced by [tags]."""

    RULES = [
        (re.compile(r"-----BEGIN [A-Z ]+-----.*?(-----END [A-Z ]+-----|$)", re.S), "[key]"),
        (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [secret]"),
        (re.compile(r"(?i)\b(bearer|token|authorization|api[_-]?key|secret|password|passwd|pwd|cookie)"
                    r"\s*[:=]\s*\S+"), r"\1=[secret]"),
        (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abpr]|AKIA|AIza|ya29|re)[-_][A-Za-z0-9_\-.]{8,}"),
         "[token]"),
        (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[token]"),
        (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[email]"),
        (re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s\"'<>)]+", re.I), "[url]"),
        (re.compile(r"(?:/home/[^/\s]+|~)/\.walter/secrets\S*"), "[secret path]"),
        (re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"), "[mac]"),
        (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[ip]"),
        (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}\b"), "[ip]"),
        (re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{1,4}:)*:(?::?[0-9a-fA-F]{1,4})+\b"), "[ip]"),
        (re.compile(r"\b[\w.-]+\.(?:ts\.net|trycloudflare\.com|wallter\.app|local)\b", re.I), "[host]"),
        (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "[hex]"),
        (re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{24,}={0,2}"), "[blob]"),
        (re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"), "[phone]"),
    ]

    def __init__(self, private_words=(), home: str | None = None, user: str | None = None):
        words = sorted({w.strip() for w in private_words if len(w.strip()) >= 3}, key=len, reverse=True)
        self.words = re.compile(r"(?i)(?<![A-Za-z0-9])(?:" + "|".join(re.escape(w) for w in words)
                                + r")(?![A-Za-z0-9])") if words else None
        self.home = home if home is not None else str(Path.home())
        self.user = user if user is not None else _user()

    @classmethod
    def from_files(cls, data_dir: Path | None = None) -> "Redactor":
        return cls(load_private_words(data_dir))

    def __call__(self, text) -> str:
        s = str(text if text is not None else "")
        if self.home and len(self.home) > 1:
            s = re.sub(re.escape(self.home) + r"/\.walter/secrets\S*", "[secret path]", s)
        for rx, sub in self.RULES:
            s = rx.sub(sub, s)
        if self.home and len(self.home) > 1:
            s = s.replace(self.home, "~")
        if self.words:
            s = self.words.sub("[name]", s)
        if self.user and len(self.user) >= 3:
            s = re.sub(r"(?i)/home/" + re.escape(self.user) + r"\b", "/home/[user]", s)
        return s


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def _data_dir(data_dir=None) -> Path:
    return Path(data_dir or os.environ.get("WALTER_DATA", Path.home() / ".walter"))


def load_private_words(data_dir: Path | None = None) -> list[str]:
    p = _data_dir(data_dir) / "private-words.txt"
    try:
        return [w.strip() for w in p.read_text(errors="replace").splitlines()
                if w.strip() and not w.strip().startswith("#")]
    except OSError:
        return []


# ------------------------------------------------------------------ collectors
def _run(argv, timeout=5) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(p) -> str:
    try:
        return Path(p).read_text(errors="replace")
    except OSError:
        return ""


def version_info(repo: Path) -> dict:
    v = {"app": (_read(repo / "app" / "VERSION").strip() or "0")}
    commit = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"]).strip()
    if re.fullmatch(r"[0-9a-f]{4,40}", commit):
        v["commit"] = commit
    real = str(repo.resolve())
    m = re.search(r"/\.walter-releases/([^/]+)", real)
    v["release"] = m.group(1) if m else "git checkout"
    return v


def system_info(data_dir: Path) -> dict:
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            try:
                mem[k] = round(int(rest.split()[0]) / 1024 / 1024, 2)
            except (ValueError, IndexError):
                pass
    cpu = ""
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            cpu = line.split(":", 1)[1].strip()
            break
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = []
    disk = {}
    for label, p in (("root", "/"), ("data", data_dir)):
        try:
            u = shutil.disk_usage(p)
            disk[label] = {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1)}
        except OSError:
            pass
    try:
        up = float(_read("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        up = 0.0
    osr = {}
    for line in _read("/etc/os-release").splitlines():
        k, _, val = line.partition("=")
        if k in ("PRETTY_NAME",):
            osr[k] = val.strip().strip('"')
    return {"os": osr.get("PRETTY_NAME") or platform.system(), "kernel": platform.release(),
            "arch": platform.machine(), "python": platform.python_version(), "cpu": cpu,
            "threads": os.cpu_count() or 0, "load": load, "ram_gb": mem, "disk": disk,
            "uptime_h": round(up / 3600, 1)}


def audio_info(red: Redactor) -> dict:
    out = {}
    for key, argv in (("capture", ["arecord", "-l"]), ("playback", ["aplay", "-l"]),
                      ("pw_sources", ["pactl", "list", "short", "sources"]),
                      ("pw_sinks", ["pactl", "list", "short", "sinks"])):
        lines = [red(l.strip())[:200] for l in _run(argv).splitlines()
                 if l.strip() and not l.startswith(" ") and ("card" in l or "\t" in l)]
        if lines:
            out[key] = lines[:20]
    return out


def service_info() -> dict:
    txt = _run(["systemctl", "--user", "show", "walter", "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic",
                "-p", "NRestarts"])
    d = dict(l.split("=", 1) for l in txt.splitlines() if "=" in l)
    info = {"state": d.get("ActiveState", "unknown")}
    try:
        mono = int(d.get("ActiveEnterTimestampMonotonic") or 0)
        if mono:
            info["up_min"] = round((time.monotonic() - mono / 1e6) / 60, 1)
    except ValueError:
        pass
    if d.get("NRestarts", "").isdigit():
        info["restarts"] = int(d["NRestarts"])
    return info


def task_states(data_dir: Path) -> dict:
    try:
        items = json.loads(_read(data_dir / "tasks.json") or "[]")
        if isinstance(items, dict):
            items = items.get("items") or items.get("tasks") or []
    except ValueError:
        items = []
    counts: dict[str, int] = {}
    recent = []
    for t in items if isinstance(items, list) else []:
        if not isinstance(t, dict):
            continue
        st = str(t.get("status") or "unknown")[:20]
        counts[st] = counts.get(st, 0) + 1
        row = {"id": t.get("id") if isinstance(t.get("id"), int) else None, "status": st}
        a, b = t.get("started") or t.get("created"), t.get("finished") or t.get("ended")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b >= a:
            row["minutes"] = round((b - a) / 60, 1)
        recent.append(row)
    return {"counts": counts, "recent": recent[-15:]}


def read_log(n: int = 2000) -> list[str]:
    txt = _run(["journalctl", "--user", "-u", "walter", "-n", str(n), "--no-pager", "-o", "short-iso"], timeout=10)
    return txt.splitlines()


ERR_RE = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception|Error:|failed)\b")


# Four or more plain words in a row = something somebody said or wrote (a transcript, a reply,
# a task title): technical lines rarely look like that, so prose is cut out of the log.
PROSE_RE = re.compile(r"[\"'\u201c\u2018]?(?:[A-Za-z][A-Za-z'\u2019\-]*[,.!?;]*\s+){3,}[A-Za-z][A-Za-z'\u2019\-]*"
                      r"[,.!?;\u2026]*[\"'\u201d\u2019]?")
QUOTED_RE = re.compile(r"'[^']{2,}'|\"[^\"]{2,}\"")


# Lines that carry what was said or written: keep the label, drop the words.
SPOKEN_RE = re.compile(r"(?i)\b(heard|partial|transcript|caption|said|saying|says|reply|ack|asked|ask|text|"
                       r"message|title|task|prompt|note|ignored|rejected)\b((?:\s*\([^)]*\))?\s*:\s).+")


def strip_prose(line: str) -> str:
    line = SPOKEN_RE.sub(lambda m: m.group(1) + m.group(2) + "[text]", line)
    return PROSE_RE.sub("[text]", QUOTED_RE.sub("[text]", line))


def log_sections(lines: list[str], red: Redactor) -> tuple[list[str], list[str]]:
    host = platform.node()
    clean = []
    for l in lines:
        if host and host.lower() not in ("walter", "localhost") and len(host) >= 3:
            l = re.sub(re.escape(host), "[host]", l, flags=re.I)
        clean.append(strip_prose(red(l))[:MAX_LINE])
    errors = [l for l in clean if ERR_RE.search(l)][-ERROR_LINES:]
    return clean[-LOG_LINES:], errors


# ------------------------------------------------------------------ bundle
def build(data_dir: Path | None = None, repo: Path | None = None, raw_config: dict | None = None,
          log_lines: list[str] | None = None, redactor: Redactor | None = None) -> dict:
    data = _data_dir(data_dir)
    repo = Path(repo or Path(__file__).resolve().parent.parent)
    red = redactor or Redactor.from_files(data)
    if raw_config is None:
        try:
            import tomllib
            with open(data / "config.toml", "rb") as f:
                raw_config = tomllib.load(f)
        except (OSError, ValueError):
            raw_config = {}
    log, errors = log_sections(read_log() if log_lines is None else log_lines, red)
    return {
        "kind": "walter-diagnostics", "schema": SCHEMA,
        "created": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "version": version_info(repo), "system": system_info(data), "audio": audio_info(red),
        "service": service_info(), "config": redacted_config(raw_config, red),
        "tasks": task_states(data), "errors": errors, "log": log,
    }


def summary(bundle: dict) -> list[str]:
    """Plain-language list of what's inside, for the consent preview."""
    v, s = bundle.get("version", {}), bundle.get("system", {})
    return [f"Walter version {v.get('app')} ({v.get('release')}, {v.get('commit', '?')})",
            f"System: {s.get('os')}, kernel {s.get('kernel')}, {s.get('threads')} threads, "
            f"{(s.get('ram_gb') or {}).get('MemTotal', '?')} GB RAM",
            f"Audio devices: {sum(len(x) for x in (bundle.get('audio') or {}).values())} lines",
            f"Settings: {sum(len(x) for k, x in (bundle.get('config') or {}).items() if isinstance(x, dict))}"
            f" technical values ({(bundle.get('config') or {}).get('_omitted_keys', 0)} personal or path"
            " values left out)",
            f"Workers: {(bundle.get('tasks') or {}).get('counts')}",
            f"Log: last {len(bundle.get('log') or [])} lines and {len(bundle.get('errors') or [])} error lines,"
            " with names, emails, addresses and keys removed"]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="walterctl diagnostics",
                                 description="Build a redacted diagnostics bundle (nothing is sent).")
    ap.add_argument("--out", help="write the bundle (JSON) to this file, mode 600")
    ap.add_argument("--json", action="store_true", help="print the whole bundle as JSON")
    a = ap.parse_args(argv)
    b = build()
    if a.out:
        p = Path(a.out).expanduser()
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(b, f, indent=1)
        print(f"wrote {p} ({p.stat().st_size} bytes). Nothing was sent; look it over before sharing it.")
    if a.json:
        print(json.dumps(b, indent=1))
    elif not a.out:
        print("Diagnostics preview (nothing is sent; --json shows everything, --out FILE saves it):")
        for line in summary(b):
            print("  - " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
