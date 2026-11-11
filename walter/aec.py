"""Barge-in and context switching: letting people talk over Walter.

Echo cancellation (deploy/pipewire/60-walter-echo-cancel.conf) gives Walter
an echo-cancelled microphone and speaker, so he can keep listening while he
talks. On a laptop speaker the canceller removes ~20 dB of his own voice but
still leaks short bursts, so nothing here trusts loudness alone: a barge-in
needs words that survive the ears' speech filters (Silero + whisper
confidence) and that are not an echo of what Walter is saying.

Modes ([ears] barge_in):
  aec   any clear speech of 2+ real words (or "stop", or his name) interrupts him
  wake  only his name (or "Walter, stop") interrupts him (no echo canceller)
  off   the old way: the mic is deaf while he talks
  auto  aec if the echo-cancelled devices exist in PipeWire, else wake
"""
from __future__ import annotations

import difflib
import logging
import re
import shutil
import subprocess

log = logging.getLogger("walter.aec")

AEC_SOURCE_NODE = "walter_aec_source"
AEC_SINK_NODE = "walter_aec_sink"

# Words that carry no meaning on their own (and backchannels: "okay", "thanks"),
# ignored when judging whether speech is an echo or a real interruption.
FILLER = set("""
a an the and or but so to of in on at for with from by is are was were be been am i
you he she it we they me my your our their this that these those there here do does
did not no yes yeah yep okay ok oh ah uh um mm hmm huh right well just really very
thanks thank cool great nice sure alright all good got it's that's i'm you're
""".split())

BACKCHANNEL_RE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|yeah|yep|yes|uh[- ]?huh|mm[- ]?hmm|mhm|right|sure|cool|great|nice|"
    r"thanks|thank you|alright|all right|got it|i see|oh|ah|wow|good|perfect)\W*){1,3}$", re.I)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in FILLER]


def echo_ratio(heard: str, spoken: list[str]) -> float:
    """Share of the heard content words that Walter himself just said (1.0 = pure echo)."""
    hw = content_words(heard)
    if not hw:
        return 1.0
    said = set(words(" ".join(spoken)))
    return sum(w in said for w in hw) / len(hw)


def is_backchannel(text: str) -> bool:
    """'okay', 'uh huh', 'thanks': listening noises, not a new request."""
    return bool(BACKCHANNEL_RE.match(text or ""))


# --------------------------------------------------------- self-echo filter
# The echo canceller leaks, and whisper (local or cloud) happily transcribes
# Walter's own voice, often slightly garbled ("the coat and QR" for "the code
# and QR"). Exact word overlap misses those, so the self-echo check matches
# words fuzzily and also compares the heard text with the best-matching stretch
# of what he said.
SELF_ECHO_MIN_WORDS = 4      # shorter texts ("stop", "yes please") are never judged self-echo
SELF_ECHO_THRESHOLD = 0.6    # token containment or sequence similarity at/above this = echo
WORD_FUZZ = 0.8              # difflib ratio for two words to count as "the same word"


def _fuzzy_in(w: str, vocab: set[str]) -> bool:
    if w in vocab:
        return True
    if len(w) < 3:
        return False
    return bool(difflib.get_close_matches(w, vocab, n=1, cutoff=WORD_FUZZ))


def new_words(text: str, spoken: list[str]) -> list[str]:
    """Content words of `text` that Walter did not just say (fuzzy, so a
    mis-transcribed echo of his own word doesn't count as new)."""
    vocab = set(words(" ".join(spoken)))
    return [w for w in content_words(text) if not _fuzzy_in(w, vocab)]


def self_echo_score(heard: str, spoken: list[str]) -> float:
    """0..1: how much `heard` looks like a (possibly garbled) copy of `spoken`.
    Max of fuzzy token containment and the sequence similarity of the heard
    words with the best-matching window of spoken words."""
    hw = words(heard)
    sw = words(" ".join(spoken))
    if not hw or not sw:
        return 0.0
    vocab = set(sw)
    contain = sum(_fuzzy_in(w, vocab) for w in hw) / len(hw)
    h = " ".join(hw)
    best = 0.0
    n = len(hw)
    for size in {max(1, n - 2), n, n + 2}:
        size = min(size, len(sw))
        for i in range(0, len(sw) - size + 1):
            r = difflib.SequenceMatcher(None, h, " ".join(sw[i:i + size]), autojunk=False).ratio()
            if r > best:
                best = r
        if best >= 0.95:
            break
    return max(contain, best)


def is_self_echo(heard: str, spoken: list[str]) -> bool:
    """Is this transcript just Walter hearing himself? Only for 4+ word texts."""
    if not spoken or len(words(heard)) < SELF_ECHO_MIN_WORDS:
        return False
    return self_echo_score(heard, spoken) >= SELF_ECHO_THRESHOLD


def barge_decision(text: str, spoken: list[str], mode: str, wake_re: re.Pattern,
                   stop_re: re.Pattern) -> tuple[bool, str]:
    """Should speech heard while Walter talks stop him? (yes/no, why)."""
    t = (text or "").strip()
    if not t or mode == "off":
        return False, "nothing"
    if is_self_echo(t, spoken):
        return False, "self-echo"
    said = " ".join(spoken)
    named = bool(wake_re.search(t))
    mine = echo_ratio(t, spoken)
    if named and not wake_re.search(said):
        return True, "his name"
    if stop_re.match(t) and not stop_re.search(said):
        return True, "stop"
    if named and mine < 0.5 and content_words(wake_re.sub("", t)):
        return True, "his name"  # he said his own name too, but these are new words
    if mode != "aec":
        return False, "no wake word"
    if is_backchannel(t):
        return False, "backchannel"
    if len(content_words(t)) < 2:
        return False, "too few words"
    if len(new_words(t, spoken)) < 2:
        return False, "too few new words"
    if mine >= 0.5:
        return False, f"echo ({mine:.0%} of the words are his)"
    return True, "voice"


def _clip(text: str, n: int, tail: bool = False) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    return "..." + text[-n:].lstrip() if tail else text[:n].rstrip() + "..."


WHAT = {
    "voice": 'what they asked: "{t}"', "text": 'what they typed: "{t}"',
    "ask": 'a message: "{t}"', "reminder": 'a reminder: "{t}"', "routine": 'a routine: "{t}"',
    "task": "a background task's report", "heartbeat": "your own check-in (heartbeat)",
    "presence": "a greeting", "firstboot": "your introduction",
}


def interruption_note(source: str, asked: str, said: list[str]) -> str:
    """The line put in front of new speech that cut Walter off mid-turn."""
    what = WHAT.get(source, '"{t}"').format(t=_clip(asked, 100))
    spoken = _clip(" ".join(said), 100, tail=True)
    if spoken:
        where = (f'You had only said this much out loud: "{spoken}"; the rest of that reply '
                 "was never heard, so don't repeat what you already said.")
    else:
        where = "None of that reply had been spoken yet."
    return (f"[They interrupted you while you were replying to {what}. {where} "
            "Drop that thread (or park it in a few words if it still matters) and answer this "
            "new thing instead. Background workers keep running.]")


# ------------------------------------------------------------ PipeWire setup
def aec_available(timeout: float = 3.0) -> bool:
    """Are the echo-cancelled source and sink running in PipeWire?"""
    exe = shutil.which("pw-cli")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "ls", "Node"], capture_output=True, text=True,
                             timeout=timeout).stdout
    except Exception as e:
        log.debug("pw-cli failed: %s", e)
        return False
    return f'"{AEC_SOURCE_NODE}"' in out and f'"{AEC_SINK_NODE}"' in out


def resolve(cfg, available=aec_available) -> str:
    """Decide the barge-in mode and point the ears/voice at the echo-cancelled
    devices when they're in use. Returns "aec", "wake" or "off"."""
    want = (cfg.ears.barge_in or "auto").lower()
    if want not in ("auto", "aec", "wake", "off"):
        log.warning("unknown [ears] barge_in %r; using auto", want)
        want = "auto"
    if want in ("off", "wake"):
        return want
    if not available():
        if want == "aec":
            log.warning("barge-in: echo canceller not found in PipeWire (see deploy/pipewire/); "
                        "only my name will interrupt me")
        return "wake"
    if not cfg.ears.device:
        cfg.ears.device = cfg.ears.aec_device
    if not cfg.voice.device:
        cfg.voice.device = cfg.voice.aec_device
    log.info("barge-in: echo canceller on (mic %s, speaker %s)", cfg.ears.device, cfg.voice.device)
    return "aec"
