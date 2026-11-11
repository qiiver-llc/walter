"""Turn Claude's streamed text into an ordered list of face/voice events.

Walter writes replies like:

    [happy] Oh hey, you're back! [action:bounce] How did the interview go?

That becomes:
    ("emotion", "happy"), ("say", "Oh hey, you're back!"),
    ("action", "bounce"), ("say", "How did the interview go?")

Text is split into sentences as it streams in, so Walter can start talking
before Claude has finished writing.
"""
from __future__ import annotations

import re

EMOTIONS = {
    "neutral", "happy", "excited", "love", "sad", "angry", "surprised", "confused",
    "thinking", "curious", "sleepy", "proud", "scared", "embarrassed", "mischievous",
    "wink", "calm",
}
ACTIONS = {
    "bounce", "hop", "pop", "wiggle", "shake", "nod", "jump", "spin", "shiver", "melt",
    "puff", "blink", "laugh", "laugh:big", "grow", "shrink", "scoot:left", "scoot:right", "peek", "look:left", "look:right", "look:up", "look:down", "look:center",
    "look:up-left", "look:up-right", "look:down-left", "look:down-right",
}

TAG_RE = re.compile(r"\[([a-zA-Z][a-zA-Z_:\- ]{0,30})\]")
# sentence end: . ! ? … followed by whitespace, or a newline
SENT_END_RE = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")
URL_RE = re.compile(r"https?://\S+")

# Words ending in "." that are not the end of a sentence. Splitting there made
# Walter say fragments ("Nov." / "4th.", "Mr." / "Smith knows?") as separate
# clips with sentence-final intonation and a pause in between: that's the
# "jumbled" speech. NEVER: never a sentence end. SOFT: only when the next word
# starts with a capital (e.g. "...at 9 a.m. Want coffee?").
NEVER_SPLIT = {
    "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "mt", "ft", "no", "vs", "e.g", "i.e",
    "approx", "fig", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
}
SOFT_SPLIT = {"a.m", "p.m", "etc", "u.s", "u.k", "inc", "ltd", "co", "min", "hr", "hrs", "sec"}
MIN_SENTENCE_WORDS = 2  # "Hmm..." or "Ha!" alone is merged into the next sentence

# Speak the first clause of a reply early: the voice synthesises a sentence
# before playing it (about real time on Walter's CPU), so a long first
# sentence is split at its first comma/semicolon/colon/dash. Only the first
# sentence (and the first after a tool call); the rest flows as sentences.
CLAUSE_RE = re.compile(r"[,;:](?=\s)|\s[—–](?=\s)")
CLAUSE_MIN_WORDS = 5        # the clause spoken early has at least this many words...
CLAUSE_MIN_TAIL = 3         # ...and so does the rest of its sentence (no "Nov. 4th." stubs)


def classify(tag: str):
    t = tag.strip().lower().replace(" ", "")
    if t in EMOTIONS:
        return ("emotion", t)
    if t.startswith("action:"):
        t = t[7:]
    if t in ACTIONS:
        return ("action", t)
    if t == "silent":
        return ("silent", None)
    return None  # unknown tag: dropped silently


def speakable(text: str) -> str:
    """Strip markdown and other things that sound silly when read aloud."""
    text = URL_RE.sub("a link", text)
    text = re.sub(r"`+", "", text)
    text = re.sub(r"\*\*|__|\*|~~|^#+\s*|^\s*[-•]\s+|^\s*\d+\.\s+", "", text, flags=re.M)
    text = EMOJI_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Things piper (espeak) reads badly, rewritten just for the voice. Captions keep
# the original text; only the audio gets this version. Measured with
# bin/tts-roundtrip: espeak ends a sentence at every "Mr." / "Nov." / "J.",
# reads "$3.50" as "dollar three point five zero" and "12:05" as "twelve zero five".
_ABBREV = {
    "mr": "Mister", "mrs": "Missus", "ms": "Miz", "dr": "Doctor", "prof": "Professor",
    "jr": "Junior", "sr": "Senior", "mt": "Mount", "approx": "about",
    "jan": "January", "feb": "February", "mar": "March", "apr": "April", "jun": "June",
    "jul": "July", "aug": "August", "sep": "September", "sept": "September",
    "oct": "October", "nov": "November", "dec": "December",
    "mon": "Monday", "tue": "Tuesday", "tues": "Tuesday", "wed": "Wednesday",
    "thu": "Thursday", "thur": "Thursday", "thurs": "Thursday", "fri": "Friday",
    "sat": "Saturday", "sun": "Sunday",
}
_FRACTIONS = {"1/2": "a half", "1/3": "a third", "2/3": "two thirds", "1/4": "a quarter",
              "3/4": "three quarters"}


def _abbrev(m):
    full = _ABBREV[m.group(1).lower()]
    return full + (m.group(2) or ".")


def _money(m):
    d, c = m.group(1), m.group(2)
    if c and int(c) and int(d.replace(",", "")) == 0:
        return f"{int(c)} cent" + ("s" if int(c) != 1 else "")
    unit = "dollar" if d == "1" else "dollars"
    return f"{d} {unit} {int(c)}" if c and int(c) else f"{d} {unit}"


def _time(m):
    h, mm, ampm = m.group(1), m.group(2), m.group(3)
    ampm = (" " + ampm.replace(".", "").replace(" ", "").upper()) if ampm else ""
    if mm == "00":
        return f"{h}{ampm}" if ampm else f"{h} o'clock"
    if mm.startswith("0"):
        return f"{h} oh {int(mm)}{ampm}"
    return f"{h} {mm}{ampm}"


_SPEECH_SUBS = [
    (re.compile(r"\be\.g\.(?=\s|$)", re.I), "for example"),
    (re.compile(r"\bi\.e\.(?=\s|$)", re.I), "that is"),
    (re.compile(r"\bvs\.?(?=\s|$)", re.I), "versus"),
    (re.compile(r"\betc\.(?=\s*$)", re.I), "et cetera."),
    (re.compile(r"\betc\b\.?", re.I), "et cetera"),
    (re.compile(r"\b(" + "|".join(_ABBREV) + r")\.(\s+(?=\S))?", re.I), _abbrev),
    (re.compile(r"\bNo\.(?=\s*\d)"), "number"),
    (re.compile(r"\bSt\.(?=\s+[A-Z])"), "Saint"),
    (re.compile(r"\b([A-Z])\.(?=\s+[A-Z])"), r"\1"),           # "J. R. Smith"
    (re.compile(r"\$(\d+(?:\.\d+)?)\s*(thousand|million|billion|trillion)\b", re.I), r"\1 \2 dollars"),
    (re.compile(r"\$(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?(?!\d)"), _money),
    (re.compile(r"\b(\d{1,2}):(\d{2})\b(\s*(?:[ap]\.m\.|[AP]\.M\.|[ap]m\b|[AP]M\b))?"), _time),
    (re.compile(r"\b([ap])\.m\.(?=\s|$)"), lambda m: m.group(1).upper() + "M"),
    (re.compile(r"(?<![\d/])(1/2|1/3|2/3|1/4|3/4)(?![\d/])"), lambda m: _FRACTIONS[m.group(1)]),
    (re.compile(r"°\s?F\b"), " degrees Fahrenheit"),
    (re.compile(r"°\s?C\b"), " degrees Celsius"),
    (re.compile(r"°"), " degrees"),
    (re.compile(r"\s*&\s*"), " and "),
    # "10-15 mph" -> "10 to 15 mph" (small numbers only: not dates or phone numbers)
    (re.compile(r"(?<![\d-])(\d{1,3})\s?[-–]\s?(\d{1,3})(?![\d-])"), r"\1 to \2"),
    # dashes as pauses: "not sure — maybe" (espeak made a hard sentence break)
    (re.compile(r"\s*[—–]\s*|\s+-\s+"), ", "),
    # ellipses: espeak swallows them and runs words together ("Hmm... I'm" -> "Am I")
    (re.compile(r"\s*(?:\.{3,}|…)\s*(?=\S)"), ", "),
    (re.compile(r"\s*(?:\.{3,}|…)\s*$"), "."),
]
# emoji and pictographs: piper reads them out ("slightly smiling face")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\U000E0000-\U000E007F]+")


def speech_text(text: str) -> str:
    """The version of a caption sentence that the voice actually reads."""
    text = EMOJI_RE.sub(" ", text)
    for rx, rep in _SPEECH_SUBS:
        text = rx.sub(rep, text)
    text = re.sub(r",\s*([,.!?])", r"\1", text)  # ", ." -> "."
    text = re.sub(r"^\s*,\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


class TagStream:
    def __init__(self, early_clause: bool = True):
        self.buf = ""
        self.in_code = False
        self.early_clause = early_clause
        self.clause_armed = early_clause  # nothing said yet: speak the first clause early

    def arm_clause(self):
        """Speak the next sentence's first clause early again (e.g. after a tool call)."""
        self.clause_armed = self.early_clause

    def feed(self, chunk: str) -> list[tuple[str, str | None]]:
        self.buf += chunk
        return self._drain(final=False)

    def flush(self) -> list[tuple[str, str | None]]:
        return self._drain(final=True)

    def _drain(self, final: bool):
        out: list[tuple[str, str | None]] = []
        while self.buf:
            # never read code blocks aloud
            if self.in_code:
                end = self.buf.find("```")
                if end < 0:
                    if final:
                        self.buf = ""
                    break
                self.buf = self.buf[end + 3:]
                self.in_code = False
                continue
            fence = self.buf.find("```")
            tag = TAG_RE.search(self.buf)
            sent = self._sentence_end(final)
            # an unfinished "[..." at the end might still become a tag
            open_br = self.buf.rfind("[")
            pending_tag = open_br >= 0 and "]" not in self.buf[open_br:] and len(self.buf) - open_br < 34

            cands = []
            if fence >= 0:
                cands.append((fence, "fence"))
            if tag:
                cands.append((tag.start(), "tag"))
            if sent:  # (False = maybe a sentence end, need more text to know)
                cands.append((sent.start(), "sent"))
            clause = self._clause_end(sent) if self.clause_armed else None
            if clause is not None:
                cands.append((clause[0], "clause"))
            if not cands:
                if final:
                    self._say(out, self.buf)
                    self.buf = ""
                elif pending_tag:
                    pass
                break
            pos, kind = min(cands)
            if kind == "fence":
                self._say(out, self.buf[:pos])
                self.buf = self.buf[pos + 3:]
                self.in_code = True
            elif kind == "clause":
                self._say(out, self.buf[:pos].rstrip() + ",")
                self.buf = self.buf[clause[1]:].lstrip()
            elif kind == "tag":
                self._say(out, self.buf[:pos])
                ev = classify(tag.group(1))
                if ev:
                    out.append(ev)
                self.buf = self.buf[tag.end():]
            else:
                self._say(out, self.buf[:sent.end()])
                self.buf = self.buf[sent.end():]
        return out

    def _sentence_end(self, final: bool):
        """The first real sentence end in the buffer: a match, None (none yet),
        or False (there may be one, but we must see more text to decide)."""
        for m in SENT_END_RE.finditer(self.buf):
            if m.group(0).startswith("\n") or "\n" in m.group(0):
                return m  # line breaks always split (lists, paragraphs)
            head = self.buf[:m.start()].rstrip("\"')]")
            word = re.split(r"[\s(\[\"']", head)[-1].lower()
            rest = self.buf[m.end():]
            if word.endswith("."):
                core = word.rstrip(".")
                if core in NEVER_SPLIT or re.fullmatch(r"[a-z]", core):
                    continue  # "Mr. Smith", "Nov. 4th", "J. Smith"
                if core in SOFT_SPLIT:
                    if not rest and not final:
                        return False
                    if rest and not rest[0].isupper():
                        continue
            if len(re.findall(r"\w+", speakable(head))) < MIN_SENTENCE_WORDS:
                if not final:
                    nxt = rest.lstrip()
                    if not nxt or TAG_RE.match(nxt) or nxt.startswith("["):
                        return False  # wait: merge with what comes next
                    continue
            return m
        return None

    def _clause_end(self, sent):
        """(end of the first clause worth speaking early, start of the rest),
        or None (no such clause in this sentence, or not yet). The
        clause is spoken with a comma, whatever punctuation ended it."""
        limit = sent.start() if sent else len(self.buf)
        for m in CLAUSE_RE.finditer(self.buf, 0, limit):
            head = self.buf[:m.start()]
            if "[" in head or "`" in head:
                return None  # a tag or code comes first: deal with that first
            # (decided by the clause alone, never by what has streamed in after
            # it, so the same reply always splits the same way)
            if len(re.findall(r"\w+", speakable(head))) < CLAUSE_MIN_WORDS:
                continue
            tail = self.buf[m.end():limit]
            known = bool(sent)
            if "[" in tail:          # a tag ends this stretch of speech
                tail, known = tail[:tail.index("[")], True
            words = re.findall(r"\w+", speakable(tail))
            if not known and words and not re.search(r"[\s,;:.!?]$", tail):
                words = words[:-1]   # the last word may still be streaming in
            if len(words) >= CLAUSE_MIN_TAIL:
                return m.start(), m.end()
            if not known:
                return None          # wait and see how long the rest is
        return None

    def _say(self, out, text):
        s = speakable(text)
        if s and re.search(r"\w", s):
            out.append(("say", s))
            self.clause_armed = False
