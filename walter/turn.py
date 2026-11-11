"""End-of-turn detection: has the person finished talking, or just paused?

Cheap, offline, CPU-free heuristics on the live partial transcript. A
sentence that ends cleanly ("What's the weather tomorrow?") gets a short
silence wait; one that trails off on a filler or connecting word ("Can you
check the weather and, um") gets a long one, so a thinking pause doesn't
cut the person off mid-thought.
"""
from __future__ import annotations

import re

COMPLETE, UNFINISHED, UNKNOWN = "complete", "unfinished", "unknown"

# Longest wait for a transcript that is still being made before the turn ends
# anyway (the person has been quiet this long: waiting more only adds delay).
PENDING_MAX_MS = 1200

# Words a sentence essentially never ends on, even if whisper slapped a full
# stop after them ("I want to check the." is a pause, not an ending).
STRONG_CONTINUERS = {
    # fillers / hesitations
    "um", "uh", "er", "erm", "uhm", "umm", "uhh", "hmm", "mm",
    # conjunctions
    "and", "but", "because", "cause", "cuz", "if", "or", "nor", "unless",
    "although", "whereas", "whether", "plus",
    # articles / determiners / possessives that need a noun
    "the", "a", "an", "my", "your", "our", "their", "every", "each",
    # prepositions that need an object
    "into", "onto",
    # contractions that need something after them
    "i'm", "i'd", "i'll", "we're", "you're", "they're", "let's", "gonna", "wanna",
}

# Words that suggest more is coming when there's no sentence punctuation,
# but can legitimately end a sentence ("I think so.", "Who am I?").
WEAK_CONTINUERS = {
    "so", "like", "to", "then", "also", "well", "for", "in", "on", "about", "of",
    "with", "from", "at", "by", "than", "that", "which", "who", "where", "when",
    "while", "though", "i", "this", "these", "those", "some", "any", "his", "its",
    "maybe", "actually", "basically", "just", "kind", "sort", "ah",
    "is", "are", "was", "were", "be", "can", "could", "would", "should", "will",
    "do", "does", "did", "have", "has", "what", "how", "why", "you", "know",
}

# Two-word endings that mean "hang on, there's more".
CONTINUER_PHRASES = ("you know", "i mean", "kind of", "sort of", "and then", "so then",
                     "as well as", "such as", "let me think", "hang on", "wait a",
                     "i think")

GREETINGS = {"hey", "hi", "hello", "ok", "okay", "oh", "so", "um", "uh", "yo", "excuse", "me"}

TRAILING_UNFINISHED =("...", "…", ",", ";", ":", "-", "–", "—")
TRAILING_COMPLETE = (".", "?", "!")

_WORD = re.compile(r"[a-z']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("’", "'"))


def classify(text: str, wake_words: list[str] | tuple[str, ...] = ()) -> tuple[str, str]:
    """(verdict, reason): does this partial transcript look like a finished turn?

    verdict is COMPLETE, UNFINISHED or UNKNOWN (no evidence either way).
    """
    t = (text or "").strip().strip("\"')]").rstrip()
    if not t:
        return UNKNOWN, "no transcript"
    words = _words(t)
    if not words:
        return UNKNOWN, "no words"

    for tail in TRAILING_UNFINISHED:
        if t.endswith(tail):
            return UNFINISHED, f"ends with {tail!r}"

    last = words[-1]
    punctuated = t.endswith(TRAILING_COMPLETE)

    if last in STRONG_CONTINUERS:
        return UNFINISHED, f"ends with {last!r}"

    # Only addressed Walter so far ("Hey Walter.") - people pause after that.
    joined = " " + " ".join(words) + " "
    if any(w and f" {w.lower()} " in joined for w in wake_words or ()):
        rest = joined
        for w in sorted(wake_words, key=len, reverse=True):
            rest = rest.replace(f" {w.lower()} ", " ")
        if not [x for x in rest.split() if x not in GREETINGS]:
            return UNFINISHED, "only the wake word so far"

    if not punctuated:
        for ph in CONTINUER_PHRASES:
            if joined.endswith(f" {ph} "):
                return UNFINISHED, f"ends with {ph!r}"
        if last in WEAK_CONTINUERS:
            return UNFINISHED, f"ends with {last!r} (no punctuation)"
        return UNKNOWN, "no sentence punctuation"

    if t.endswith("?"):
        return COMPLETE, "ends with a question mark"
    return COMPLETE, f"ends with {t[-1]!r}"


def looks_unfinished(text: str, wake_words: list[str] | tuple[str, ...] = ()) -> bool:
    return classify(text, wake_words)[0] == UNFINISHED


def silence_needed_ms(text: str, ears_cfg) -> tuple[int, str, str]:
    """(ms of silence that ends the turn, verdict, reason) for this transcript."""
    if not getattr(ears_cfg, "adaptive_end_of_turn", True):
        return ears_cfg.end_silence_ms, UNKNOWN, "adaptive end-of-turn off"
    verdict, reason = classify(text, getattr(ears_cfg, "wake_words", ()))
    if verdict == COMPLETE:
        ms = ears_cfg.end_silence_complete_ms
    elif verdict == UNFINISHED:
        ms = ears_cfg.end_silence_unfinished_ms
    else:
        ms = ears_cfg.end_silence_ms
    return ms, verdict, reason


def end_of_turn(silence_ms: int, text: str | None, ears_cfg,
                pending: bool = False) -> tuple[bool, str, str]:
    """Should the turn end after `silence_ms` of silence? (end, verdict, reason)

    text: a transcript covering *all* the speech so far, or None if there is
    none yet. pending: such a transcript is being made right now (the early
    final). Waiting for it costs no latency, because the final transcript
    would have to wait for the same whisper pass anyway.
    """
    if not getattr(ears_cfg, "adaptive_end_of_turn", True):
        return silence_ms >= ears_cfg.end_silence_ms, UNKNOWN, "adaptive end-of-turn off"
    longest = max(ears_cfg.end_silence_ms, ears_cfg.end_silence_unfinished_ms)
    if text is None:
        if pending:
            if silence_ms >= min(longest, max(PENDING_MAX_MS, ears_cfg.end_silence_ms)):
                return True, UNKNOWN, "transcript not ready in time"
            return False, UNKNOWN, "transcript still being made"
        return silence_ms >= ears_cfg.end_silence_ms, UNKNOWN, "no up-to-date transcript"
    need, verdict, reason = silence_needed_ms(text, ears_cfg)
    return silence_ms >= min(need, longest), verdict, reason
