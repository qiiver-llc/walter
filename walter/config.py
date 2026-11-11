"""Configuration: defaults, overridden by a TOML file (~/.walter/config.toml)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8765
    setup_port: int = 8766             # LAN-facing first-time setup page (only while not set up)


@dataclass
class BrainCfg:
    claude_bin: str = "claude"
    model: str = ""                    # empty = Claude Code's default
    # Automatic model switching (router.py): the fast model chats, the deep one
    # takes hard turns. fast_model "" = model. auto_switch = false: always fast.
    fast_model: str = ""
    deep_model: str = "opus"
    auto_switch: bool = True
    # Headless Claude Code can't ask you for permission, so it needs a mode
    # that never prompts. "bypassPermissions" lets Walter run any command
    # and edit any file the walter user can: that is what makes him able to
    # program himself. Use "acceptEdits" + allowed_tools to restrict him.
    permission_mode: str = "bypassPermissions"
    allowed_tools: list[str] = field(default_factory=list)
    # Deny rules (Claude Code --disallowedTools); they bind in every mode, bypass too.
    # Default empty = unchanged behaviour; fresh installs get walter.security's preset.
    disallowed_tools: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    home: str = str(DATA_DIR / "home")  # Walter's working dir (CLAUDE.md, memory, skills)
    turn_timeout: int = 600             # seconds before a stuck turn is killed
    # background tasks (separate Claude Code workers for long jobs)
    max_tasks: int = 3
    task_timeout: int = 3600
    task_model: str = ""                # "" = same as model
    # Workers run at low CPU/IO priority so Walter's voice (piper, PipeWire) stays
    # clean while they build or drive Chromium. Applied per worker at launch (read
    # fresh from config.toml, so a change counts from the next worker on).
    # cpu/io weight 1-10000 (systemd default 100), nice 0-19. 100/100/0 = normal.
    worker_cpu_weight: int = 20
    worker_io_weight: int = 20
    worker_nice: int = 10


@dataclass
class EarsCfg:
    enabled: bool = True
    device: str = ""                   # sounddevice input name/index; "" = default
    whisper_model: str = "base.en"     # tiny.en is faster on very old CPUs
    whisper_compute: str = "int8"
    whisper_threads: int = 0           # CPU threads per whisper pass (0 = library default);
                                       # 2 is fastest on a 2-core/4-thread laptop
    whisper_nice: int = 10             # whisper's compute threads run at this niceness (0-19), so
                                       # the voice (piper) and PipeWire win the CPU; 0 = normal
    # Final transcript of each turn: a bigger, more accurate model ("" = whisper_model).
    # Live partials keep using the fast whisper_model.
    final_model: str = ""
    # Vocabulary hints: names and words whisper should expect (spelled as you want them).
    # Add your household's names, town, pets... in ~/.walter/config.toml.
    hotwords: list[str] = field(default_factory=lambda: ["Walter", "Claude", "GitHub"])
    vad_aggressiveness: int = 2        # 0-3
    end_silence_ms: int = 800          # silence that ends a turn when the words give no hint
    # --- end-of-turn detection (see turn.py) ---
    adaptive_end_of_turn: bool = True  # judge from the live transcript whether they've finished
    end_silence_complete_ms: int = 700  # ...if it sounds finished ("What time is it?")
    end_silence_unfinished_ms: int = 2000  # ...if it trails off ("and", "um", "because", ",")
    max_utterance_s: float = 90.0      # longest single turn (long turns are transcribed as you go)
    chunk_s: float = 12.0              # long turns: transcribe the audio so far every ~this often
    # --- ambient-noise filtering (see ears.py) ---
    noise_gate_ratio: float = 1.5      # once speaking, a frame only keeps the utterance going if it is
                                       # this many times louder than the room (adaptive); 0 = off
    min_speech_ms: int = 200           # drop utterances with less actual speech than this
    silero_vad: bool = True            # second, stronger VAD pass inside faster-whisper
    silero_threshold: float = 0.4      # 0-1, higher = stricter (0.5 dropped quiet far-field speech)
    # Silero rescue: when Silero hears no speech in a long final/piece that is
    # clearly louder than the room (and Walter wasn't talking), let whisper's
    # own no-speech check decide instead (one extra pass, finals only).
    silero_rescue: bool = True
    silero_rescue_min_s: float = 3.0   # only segments at least this long
    silero_rescue_ratio: float = 2.5   # a frame counts as loud at this many times the background
    silero_rescue_loud_s: float = 1.0  # ...and the segment needs this much loud audio
    silero_rescue_no_speech: float = 0.5  # stricter no_speech_prob limit for rescued segments
    max_no_speech_prob: float = 0.7    # drop whisper segments more likely silence than this
    min_avg_logprob: float = -1.0      # drop whisper segments it is this unsure about
    max_compression_ratio: float = 2.4 # drop repetitive "loop" hallucinations
    # --- live transcription (see ears.py) ---
    live_partials: bool = True         # show words on the face while the person is still talking
    partial_interval_s: float = 1.2    # how often to re-transcribe the growing utterance
    partial_max_s: float = 15.0        # stop live updates for utterances longer than this (CPU)
    early_final: bool = True           # start the final transcription after 0.3 s of silence,
                                       # so the reply comes sooner (same result)
    beam_size: int = 3                 # final transcript: higher = a bit more accurate, slower (1 = fastest)
    wake_words: list[str] = field(default_factory=lambda: ["walter", "wolter", "walther", "hey walt"])
    require_wake_word: bool = True
    followup_window_s: float = 12.0    # after Walter talks, no wake word needed
    startup_window_s: float = 60.0  # after a (re)start, listen without the wake word this long
    # --- barge-in: talking over Walter stops him (see aec.py) ---
    # "auto": full barge-in if the PipeWire echo canceller is running
    #         (deploy/pipewire/), else only his name interrupts him
    # "aec":  full barge-in (any clear speech that isn't his own echo)
    # "wake": only his name (or "stop") interrupts him; the mic stays on while he talks
    # "off":  the old way: the mic is deaf while he talks
    barge_in: str = "auto"
    barge_in_min_ms: int = 450         # speech needed (while he talks) before checking for barge-in
    # While he talks, barge-in checks transcribe only the newest audio (not the whole,
    # growing echo), and only when it is clearly louder than his own echo usually is.
    barge_in_window_s: float = 3.0     # seconds of the newest audio each barge-in check hears
    barge_in_energy_ratio: float = 1.6 # check at once if the last ~0.6 s is this many times louder
                                       # than his typical echo (learned while he talks); 0 = no gate
    barge_in_recheck_s: float = 3.0    # quieter than that: still check every this often (safety
                                       # net for soft voices); 0 = never
    aec_device: str = "walter_aec_mic" # ALSA name of the echo-cancelled mic (used when device = "")


@dataclass
class VoiceCfg:
    enabled: bool = True
    device: str = ""                   # sounddevice output name/index
    aec_device: str = "walter_aec_speaker"  # echo-cancelled output, used when device = "" and AEC runs
    piper_model: str = str(DATA_DIR / "voices" / "en_GB-alan-medium.onnx")
    length_scale: float = 1.0          # >1 slower, <1 faster
    volume: float = 1.0
    pitch: float = 1.0                 # <1 deeper, >1 higher (speed is kept)
    # Output buffer asked of PipeWire per sentence. PortAudio's default (~35 ms, a
    # 192-sample node) xruns under CPU load on the 1024/48000 echo-cancel graph: the
    # garbled first sentence (journal/2026-11-05-performance-rootcause.md). 0 = default.
    output_latency_s: float = 0.2
    laugh_sounds: bool = True          # [laugh] also plays a recorded chuckle (sounds/laugh/)
    # Local voice engine (trial): "piper" (default) or "kokoro" (Kokoro-82M via onnxruntime,
    # more natural but much slower on a 2-core CPU; piper is the automatic fallback)
    engine: str = "piper"
    kokoro_dir: str = str(DATA_DIR / "voices" / "kokoro" / "kokoro-int8-multi-lang-v1_0")
    kokoro_voice: str = "am_michael"   # American male: am_michael, am_puck, am_fenrir, am_adam ...
    kokoro_speed: float = 1.1          # >1 faster
    kokoro_threads: int = 2


@dataclass
class SpeechCfg:
    # Local or cloud speech (speech.py). These are only the starting defaults:
    # "walterctl voice local|openai" / "walterctl ears local|openai" switch live
    # and are remembered in ~/.walter/speech.json, which wins over these.
    tts_provider: str = "local"        # "local" (piper) | "openai"
    stt_provider: str = "local"        # "local" (faster-whisper) | "openai"
    key_file: str = ""                 # "" = ~/.walter/secrets/openai.key (else $OPENAI_API_KEY)
    openai_base_url: str = "https://api.openai.com/v1"
    tts_model: str = "gpt-4o-mini-tts" # or "tts-1" (cheaper per character, no instructions)
    tts_voice: str = "ash"             # warm male voices: ash, onyx, echo, ballad
    tts_speed: float = 1.0             # 0.25-4 (tts-1 / tts-1-hd only)
    tts_pitch: float = 1.0             # <1 deeper (tts-1 voices only; tempo is kept)
    tts_tempo: float = 1.0             # local speed-up of cloud speech, pitch kept (e.g. 1.15)
    tts_instructions: str = ("Warm, friendly and relaxed, a little playful, like a good friend "
                             "chatting in the living room. Natural pace.")  # gpt-4o-mini-tts only
    tts_timeout_s: float = 6.0         # slower than this: one quick retry (below), then fallback
    # A failed cloud sentence is retried once with this (shorter) timeout. If that fails too,
    # a reply never switches voice mid-way: before any cloud audio the whole reply goes to
    # piper; after cloud audio that sentence is shown as a caption only (voice.py).
    tts_retry_timeout_s: float = 4.0   # 0 = no retry
    stt_model: str = "gpt-4o-mini-transcribe"  # or "whisper-1", "gpt-4o-transcribe"
    stt_timeout_s: float = 8.0         # slower than this: the local transcript is used
    # Cost safety: once a cap is reached, that side falls back to local (0 = no cap).
    max_tts_chars_per_day: int = 20000     # roughly 20 minutes of talking
    max_stt_seconds_per_day: float = 1800  # 30 minutes of listening
    max_tts_chars_per_month: int = 300000
    max_stt_seconds_per_month: float = 30000
    # --- Hybrid realtime mode (realtime.py): "walterctl mode classic|realtime".
    # Starting default only; the live switch is remembered in ~/.walter/speech.json.
    mode: str = "classic"              # "classic" (brain + voice) | "realtime" (OpenAI Realtime front end)
    realtime_model: str = "gpt-realtime-2.1-mini"
    realtime_url: str = "wss://api.openai.com/v1/realtime"
    realtime_voice: str = "ash"        # warm male: ash, ballad, verse, echo, cedar (fixed per session)
    realtime_speed: float = 1.0        # 0.25-1.5
    # Turns are gated by the local ears (server turn detection is off, so the
    # server never answers noise by itself); these three are no longer used.
    realtime_vad: str = "server_vad"
    realtime_vad_threshold: float = 0.5
    realtime_silence_ms: int = 500
    realtime_input: str = "audio"      # confirmed utterances go as "audio" or as the local "text" transcript
    realtime_max_output_tokens: int = 600  # per reply (~20 audio tokens a second); 0 = no limit
    realtime_noise_reduction: str = "far_field"  # "near_field" | "far_field" | "" (off)
    realtime_idle_close_s: float = 60.0  # close the session after this long with nothing said
    realtime_tool_timeout_s: float = 120.0  # longest ask_walter wait before giving up
    realtime_fallback_min: float = 10.0  # after an error, use classic for this long
    # Cost safety: minutes of open (streaming) realtime session; 0 = no cap.
    max_realtime_min_per_day: float = 20.0
    max_realtime_min_per_month: float = 300.0


@dataclass
class LifeCfg:
    heartbeat_minutes: int = 120       # 0 = never speak unprompted
    # the wake-up line of each beat ("" = built-in; {time} {home} filled in; see walter/heartbeat.py). Live.
    heartbeat_prompt: str = ""
    quiet_hours: list[int] = field(default_factory=lambda: [23, 6])  # may doze off from 23:00 to 06:00
    sleep_after_idle_min: int = 15     # ...after this long without anyone talking to him
    muted_sleep_min: float = 5.0  # mic muted this long (and idle): fall asleep; Space wakes (0 = never)
    captions: bool = False
    # sleep-time learning (walter/sleeplearn.py): a quiet self-review once per sleep
    sleep_review: bool = True
    sleep_review_settle_min: float = 10.0   # asleep this long first (short naps don't count)
    sleep_review_gap_h: float = 6.0         # and not again within this many hours
    sleep_review_model: str = "sonnet"      # cheap model for the review worker ("" = task_model)


@dataclass
class SensesCfg:
    # Webcam presence ("is someone in the room?"), see presence.py. Frames stay in
    # memory at low resolution and are never saved or sent; face recognition
    # only with recognise_faces and only for people who enrolled.
    # The camera is switched off entirely during quiet hours.
    camera: bool = False               # off unless you turn it on in ~/.walter/config.toml
    camera_device: str = "0"           # /dev/video0 ("0"), or a path
    width: int = 320
    height: int = 240
    fps: float = 1.0                   # frames looked at per second (0.2-5)
    arrive_after_min: float = 10.0     # someone appearing after this long unseen = an arrival
    empty_after_min: float = 15.0      # nobody seen for this long: doze (visually; mic stays on)
    greet_cooldown_min: float = 30.0   # at most one arrival greeting this often
    debug: bool = False                # log (text only) which signal made the camera think someone's there
    # Sound-triggered camera sleep: the camera is released (LED off) once nobody
    # has been seen for a while and the room is quiet; a sound that clearly
    # stands out from the mic's adaptive background (or a voice) wakes it to
    # check for a person for verify_s seconds.
    camera_sleep: bool = True          # false = keep watching all day (the old behaviour)
    sleep_after_min: float = 5.0       # nobody seen this long + quiet mic: camera sleeps
    verify_s: float = 45.0             # how long a sound-woken camera looks for a person
    wake_ratio: float = 3.0            # a sound this many times the background wakes it...
    wake_cooldown_s: float = 60.0      # ...but not again this soon after a check found nobody (doubles, max 10 min)
    # Who is there (see people.py): opt-in, local, embeddings only. Nobody is
    # recognised unless they enrolled ("walterctl enroll face|voice NAME").
    recognise_faces: bool = False      # match faces the camera sees against enrolled people
    recognise_voices: bool = False     # label each heard utterance with the enrolled speaker
    face_threshold: float = 0.0        # 0 = per person, tuned at enrolment (0.42-0.55)
    voice_threshold: float = 0.0       # 0 = per person, tuned at enrolment (0.50-0.70)


# Calendar events whose title or location contains one of these words (whole words; a
# plural "s" is fine) get a small weather chip on the dashboard. An event's own
# "outdoor": true/false (set-events JSON) always wins.
DEFAULT_OUTDOOR_KEYWORDS = [
    "football", "soccer", "baseball", "softball", "game", "games", "playoff", "playoffs",
    "track", "cross country", "practice", "parade", "fair", "cookout", "bbq", "barbecue",
    "picnic", "golf", "field", "park", "outdoor", "outdoors", "outside", "farm", "trailer",
    "rodeo", "livestock", "4-h", "hunt", "hunting", "fishing", "camp", "camping", "hike",
    "walk", "5k", "garden", "yard", "pumpkin", "harvest", "tailgate", "fireworks",
    "zoo", "lake", "pool", "beach", "festival", "farmers market",
]


@dataclass
class DashboardCfg:
    """Dashboard mode (face/dashboard.js) while Walter sleeps."""
    enabled: bool = True
    lat: float = 0.0                   # weather location (set yours in ~/.walter/config.toml)
    lon: float = 0.0
    list_title: str = "My list"        # heading of the person's list on the wall
    dark_hours: list[int] = field(default_factory=lambda: [23, 5])  # only a dim clock
    # Phone page http://<lan-ip>:PORT/dashboard/add. When server.host is loopback the
    # server also listens on the LAN, but answers nothing except the dashboard page and
    # /api/dashboard there, and only to private (home network) addresses.
    phone_page: bool = True
    # weather chip next to outdoor events (see DEFAULT_OUTDOOR_KEYWORDS above)
    outdoor_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_OUTDOOR_KEYWORDS))
    # small sports row above the portrait 7-day strip: "league/team" (ESPN slugs + team id
    # or abbreviation), e.g. ["nfl/gb", "mlb/nyy"]. [] (default) hides it.
    sports_teams: list[str] = field(default_factory=list)
    sports_rows: int = 2               # at most this many games shown at once
    # High school teams in the same row (walter/hs_sports.py; currently NSAA public pages),
    # inline tables: { sport = "football"|"volleyball", school = "...", class = "C1", sid = 101,
    # abbr = "ABC" } (+ optional name, color, schedule, bracket). `walter-dashboard hs-find`.
    hs_teams: list = field(default_factory=list)
    # Fixed colours for calendar labels (the "calendar" field of set-events), e.g.
    # { Main = "#5fa8ff", Family = "#6fd49a" }; labels not listed get a hashed hue.
    calendar_colors: dict = field(default_factory=dict)
    # Show today's notes item "Verse: <text> - <reference>" (or any daily line written by a
    # routine) as a calm line under the clock. Off by default.
    verse_line: bool = False


@dataclass
class AppCfg:
    """Companion app for phone and computer (app/, served at /app; app/README.md)."""
    enabled: bool = True
    # Address the pairing QR code points at, e.g. "https://walter.tail1234.ts.net" once
    # Tailscale serve or a Cloudflare Tunnel is set up. "" = http://<lan-ip>:<port>.
    public_url: str = ""
    # Devices on the home network may open /app directly (plain http: no microphone).
    # Paired-device tokens are required either way; false = only via the proxy/localhost.
    allow_lan: bool = True
    max_talk_s: float = 60.0           # longest push-to-talk recording


@dataclass
class CloudLinkCfg:
    """Cloud Link: the app from anywhere via a private <slug>.wallter.app tunnel
    (walter/cloud_link.py, docs/CLOUD-LINK.md). Off until `walterctl cloud-link enable CODE`."""
    api_base: str = "https://api.wallter.app"   # the Cloud Link service
    heartbeat_h: float = 6.0           # signed "still here" ping while it is on
    health_timeout_s: float = 60.0     # how long enable waits for https://<host>/app/ to answer


@dataclass
class FacesCfg:
    """One brain, many faces (walter/faces.py, docs/FACES.md)."""
    enabled: bool = True
    wall_name: str = "Walter"          # this wall's own face name (plain "Walter" = best mic)
    window_s: float = 1.2              # same utterance from several faces within this = one turn
    match: float = 0.6                 # fuzzy text match (0..1) that counts as the same utterance
    followup_s: float = 10.0           # a face keeps listening without its name this long after a reply
    ears_timeout_s: float = 60.0       # a face with ears counts as listening this long after its last hello
    mdns: bool = True                  # advertise _walter._tcp on the home network (needs python zeroconf)


@dataclass
class ClusterCfg:
    """Several full installs as one brain (walter/cluster.py, docs/CLUSTER.md). Does nothing
    until `walterctl cluster pair` / `join`: with no peers the install is "solo" as before."""
    enabled: bool = True
    name: str = ""                     # this install's name in the cluster ("" = [faces] wall_name)
    priority: int = 50                 # 0..100: higher is preferred as lead at election time
    url: str = ""                      # how peers reach me ("" = http://<lan ip>:<port>)
    heartbeat_s: float = 3.0           # heartbeat interval; a lead is gone after `missed` of them
    missed: int = 3
    sync_s: float = 15.0               # followers pull shared state this often (and when poked)
    jobs: bool = True                  # run workers for other installs (walterctl task --on)
    mdns: bool = True                  # advertise _walter-cluster._tcp (needs python zeroconf)


@dataclass
class FeedbackCfg:
    """Beta feedback (walter/feedback.py). Nothing is ever sent unless the owner taps Send."""
    # Where "Send to the Walter team" POSTs, e.g. "https://wallter.app/api/feedback".
    # "" = keep it on this box and offer a downloadable file to email instead.
    endpoint: str = ""


@dataclass
class HomeAssistantCfg:
    """Optional bridge to an existing Home Assistant (walter/homeassistant.py,
    docs/HOME-ASSISTANT.md). Token: ~/.walter/secrets/homeassistant.token (mode 600)."""
    enabled: bool = False
    url: str = ""                      # e.g. "http://homeassistant.local:8123"
    timeout_s: float = 6.0
    fastpath: bool = True              # "turn off the kitchen lights" without a brain round-trip


@dataclass
class RadioCfg:
    # presets: "Name" (looked up on radio-browser.info) or "Name=https://stream/url"
    favorites: list[str] = field(default_factory=list)
    volume: int = 80          # mpv volume 0-100
    duck_volume: int = 25     # percent of volume while I listen/think


@dataclass
class SpotifyCfg:
    name: str = "Walter"      # the speaker name shown in the Spotify app
    bitrate: int = 320        # 96 | 160 | 320
    duck_volume: int = 25     # percent while I listen/think (muted while I talk)


@dataclass
class AddonsCfg:
    """Add-ons (walter/addons.py, docs/ADDONS.md): the catalog index + the repo/branch installs are pinned from."""
    catalog: str = "https://raw.githubusercontent.com/qiiver-llc/walter-addons/main/index.json"
    repo: str = "qiiver-llc/walter-addons"
    branch: str = "main"


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    brain: BrainCfg = field(default_factory=BrainCfg)
    ears: EarsCfg = field(default_factory=EarsCfg)
    voice: VoiceCfg = field(default_factory=VoiceCfg)
    speech: SpeechCfg = field(default_factory=SpeechCfg)
    life: LifeCfg = field(default_factory=LifeCfg)
    senses: SensesCfg = field(default_factory=SensesCfg)
    dashboard: DashboardCfg = field(default_factory=DashboardCfg)
    app: AppCfg = field(default_factory=AppCfg)
    cloud_link: CloudLinkCfg = field(default_factory=CloudLinkCfg)
    faces: FacesCfg = field(default_factory=FacesCfg)
    cluster: ClusterCfg = field(default_factory=ClusterCfg)
    feedback: FeedbackCfg = field(default_factory=FeedbackCfg)
    homeassistant: HomeAssistantCfg = field(default_factory=HomeAssistantCfg)
    radio: RadioCfg = field(default_factory=RadioCfg)
    spotify: SpotifyCfg = field(default_factory=SpotifyCfg)
    addons: AddonsCfg = field(default_factory=AddonsCfg)

    @property
    def data_dir(self) -> Path:
        return DATA_DIR


# Older names for the same settings (config files written for earlier versions).
ALIASES = {
    "live_transcripts": "live_partials",
    "partial_every_s": "partial_interval_s",
    "pause_ms": "end_silence_unfinished_ms",
}
IGNORED = {"noise_margin_db"}  # replaced by noise_gate_ratio; accepted so old files still load


def _apply(obj, values: dict):
    names = {f.name: f for f in fields(obj)}
    for key, val in values.items():
        if key in IGNORED and key not in names:
            continue
        key = ALIASES.get(key, key) if key not in names else key
        if key not in names:
            raise ValueError(f"unknown config key: {key}")
        cur = getattr(obj, key)
        if is_dataclass(cur):
            _apply(cur, val)
        else:
            if isinstance(val, str):
                val = os.path.expanduser(val)
            setattr(obj, key, val)


def load(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    path = Path(path) if path else DATA_DIR / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f))
    return cfg


def secrets_path() -> Path:
    """Legacy KEY=VALUE file from older setups (only ANTHROPIC_API_KEY is still honoured)."""
    return DATA_DIR / "secrets.env"


def anthropic_key_path() -> Path:
    """The owner's own Anthropic API key, if they chose that over signing in."""
    return DATA_DIR / "secrets" / "anthropic.key"


def save_secret(path: Path, value: str):
    """Write a secret with mode 600 in a mode-700 folder (atomic; never logged)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def claude_env(cfg: "Config") -> dict[str, str]:
    """Environment for every Claude Code process Walter starts.

    Adds walterctl to PATH and, if the owner chose their own Anthropic API key,
    ANTHROPIC_API_KEY (from ~/.walter/secrets/anthropic.key). Claude sign-in
    itself is Claude Code's own business: Walter never stores or passes Claude
    login tokens (a CLAUDE_CODE_OAUTH_TOKEN in an old secrets.env is ignored).
    """
    env = dict(os.environ)
    env["PATH"] = str(REPO_DIR / "bin") + os.pathsep + env.get("PATH", "")
    env["WALTER_URL"] = f"http://{cfg.server.host}:{cfg.server.port}"
    try:
        for line in secrets_path().read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                env["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
    except (FileNotFoundError, PermissionError):
        pass
    try:
        key = anthropic_key_path().read_text().strip()
        if key:
            env["ANTHROPIC_API_KEY"] = key
    except (FileNotFoundError, PermissionError):
        pass
    return env
