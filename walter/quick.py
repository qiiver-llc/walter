"""Quick decisions that don't need the brain (UX audit items 1 and 2, 5 Nov 2026).

* Is an utterance a clear request for Walter (so it may cancel a turn that's
  still thinking), or room chatter that should only ride along as context?
* "Are you there?" while he's busy: answer at once instead of cancelling.
* Did the new words clearly replace an unanswered request ("never mind",
  "forget that")?
* Instant acknowledgements ("One sec.") for turns that will use tools.
* The local fast path: a few common commands matched with strict, full-line
  patterns and carried out without a brain round-trip. Anything not matched
  exactly falls through to the brain.

Pure functions only (no I/O): the conductor carries the commands out.
"""
from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------- normalising
FILLER_START = re.compile(
    r"^(?:(?:hey|hi|ok(?:ay)?|so|um+|uh+|oh|and|well|alright|all right|right|yo|now|actually|"
    r"also|just|then|but|look|listen)\b[\s,.!-]*)+", re.I)
POLITE_START = re.compile(
    r"^(?:(?:please|can you|could you|would you|will you|can u|would you mind|i need you to|"
    r"i want you to|i'd like you to|go ahead and)\b[\s,]*)+", re.I)
POLITE_END = re.compile(r"[\s,]*\b(?:please|thanks|thank you|for me|now|real quick|buddy|mate)\s*$", re.I)


def strip_name(text: str, wake_re: re.Pattern | None = None) -> str:
    """'Hey Walter, can you add eggs to my list please?' -> 'add eggs to my list'."""
    t = text.strip()
    if wake_re is not None:
        t = wake_re.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.!?;:-")
    for _ in range(3):
        before = t
        t = FILLER_START.sub("", t).strip(" ,.!?;:-")
        t = POLITE_START.sub("", t).strip(" ,.!?;:-")
        t = POLITE_END.sub("", t).strip(" ,.!?;:-")
        if t == before:
            break
    return t


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


# ------------------------------------------------------------ addressed or not
QUESTION_START = re.compile(
    r"^(what|what's|whats|who|who's|when|when's|where|where's|why|how|how's|which|can|could|would|"
    r"will|do|does|did|is|are|was|were|should|have|has|had|any|tell me|are you|you)\b", re.I)
COMMAND_START = re.compile(
    r"^(add|put|remind|set|turn|play|pause|resume|show|tell|check|find|look|open|close|read|give|"
    r"make|start|stop|cancel|switch|mute|unmute|call|text|send|search|google|schedule|book|write|"
    r"create|delete|remove|change|move|go|wake|take|let's|lets|note|list|repeat|say|explain|"
    r"summari[sz]e|research|compare|fix|build|update|clear|hide|scroll|pull up|bring up|plan|"
    r"help|try|keep|forget|ignore|skip|continue|carry on|finish|undo|use|run|restart)\b", re.I)
PLEASE_RE = re.compile(r"\b(please|can you|could you|would you|i need you to|i want you to|"
                       r"i'd like you to)\b", re.I)
LONG_ASIDE_WORDS = 25   # an unnamed ramble this long while he's busy is talk in the room


def is_clear_request(text: str) -> bool:
    """A request, command or question plainly meant for Walter, judged without
    his name. Statements and fragments ("There goes the Wi-Fi again.", "All
    kinds of stuff.") are not."""
    raw = text.strip()
    if not raw or len(words(raw)) > LONG_ASIDE_WORDS:
        return False
    if PLEASE_RE.search(raw):
        return True
    t = strip_name(raw)
    if not t:
        return False
    n = len(words(t))
    if COMMAND_START.match(t) and n >= 2:
        return True
    if QUESTION_START.match(t) and (raw.rstrip().endswith("?") or n >= 3):
        return True
    return False


PRESENCE_CHECK = re.compile(
    r"^(?:are you (?:there|here|awake|listening|alive|ok(?:ay)?|with me|still there|working)|"
    r"you there|you still there|you awake|you listening|hello|hello there|"
    r"can you hear me|do you hear me|did you hear me|are you hearing me|hear me|you hear me)$", re.I)


def is_presence_check(text: str, wake_re: re.Pattern | None = None) -> bool:
    """'Walter, are you there?' / 'can you hear me?': nothing to work on, just
    wants to know he's alive."""
    return bool(PRESENCE_CHECK.match(strip_name(text, wake_re)))


REPLACED_RE = re.compile(
    r"\b(never ?mind|nevermind|forget (?:that|it|about (?:that|it))|scratch that|ignore that|"
    r"cancel that|don'?t (?:bother|worry about (?:that|it))|instead)\b", re.I)


def replaces_previous(text: str) -> bool:
    """The new words clearly drop whatever they'd asked before."""
    return bool(REPLACED_RE.search(text))


# ------------------------------------------------------------ acknowledgements
ACK_LINES = ["On it.", "One sec.", "Let me look.", "Give me a moment.", "Let me check.",
             "Just a sec.", "Checking now."]
TOOLISH = re.compile(
    r"\b(weather|forecast|rain|snow|temperature|calendar|schedule|agenda|appointment|meeting|event|"
    r"email|e-mail|inbox|mail|remind\w*|reminder|add|put|note|list|search|look (?:up|into|at)|"
    r"find|check|google|news|score|scores|result|results|election|research|build|fix|make|write|"
    r"create|set up|install|update|change|turn (?:on|off|up|down)|lights?|task|worker|status|"
    r"latest|when is|when's|what's on|price|"
    r"cost|open|show me|play|traffic|game|commit|push|restart|canvas|card|dashboard|memory|"
    r"remember|how much|how many|how long|compare|plan|summari[sz]e)\b", re.I)
CHAT = re.compile(
    r"^(?:hi|hello|hey|thanks|thank you|cheers|good (?:morning|night|evening|afternoon)|yes|yeah|"
    r"yep|no|nope|ok(?:ay)?|cool|nice|great|awesome|lol|wow|ha+|sure|fine|right|good|perfect|"
    r"how are you|how's it going|what's up|tell me a joke|i love you|good job|well done)\b", re.I)


# Questions and requests: in practice (6-9 Nov logs) almost all of them reach for a
# tool and take 5-20 s, so they get an instant acknowledgement too.
ASKING = re.compile(
    r"^(?:(?:so|and|okay|ok|hey|well|now|also|oh),?\s+)*(?:what|what's|whats|who|who's|where|where's|when|"
    r"why|how|which|is|are|was|were|did|does|do|can|could|would|will|should|has|have|tell me|find|look|"
    r"check|give me|show me|play|put|turn|open|set|send|read|get|go ahead|please|let's|i need|i want|"
    r"start|run|keep|move|draft|book|order|call|text|email|message|schedule|cancel|delete|clear)\b", re.I)


LEAD_NAME = re.compile(r"^\W*(?:(?:hey|hi|ok|okay|so|oh)\W+)?walter\b", re.I)


def needs_ack(text: str, deep: bool = False) -> bool:
    """Will this turn likely use tools or take more than a few seconds? Then a
    quick spoken acknowledgement beats silence. Skipped for trivial chat."""
    t = strip_name(text, LEAD_NAME)
    n = len(words(t))
    if n < 3:
        return False
    if CHAT.match(t) and n <= 6:
        return False
    return deep or bool(TOOLISH.search(t)) or (n >= 4 and (bool(ASKING.match(t))
                                                          or text.rstrip().endswith("?")))


class Picker:
    """Random choice that never repeats the last pick."""

    def __init__(self, options: list[str]):
        self.options = list(options)
        self.last: str | None = None

    def pick(self) -> str:
        choices = [o for o in self.options if o != self.last] or self.options
        self.last = random.choice(choices)
        return self.last


# --------------------------------------------------------------- fast path
NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
             "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
             "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty five": 25, "twenty-five": 25,
             "thirty": 30, "forty": 40, "forty five": 45, "forty-five": 45, "fifty": 50,
             "sixty": 60, "ninety": 90, "a couple of": 2, "a couple": 2, "couple of": 2}
NUM = r"(\d{1,3}|" + "|".join(sorted((re.escape(k) for k in NUM_WORDS), key=len, reverse=True)) + r")"
UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"


def _num(s: str) -> int | None:
    s = s.lower().strip()
    if s.isdigit():
        return int(s)
    return NUM_WORDS.get(s)


def _secs(n: int, unit: str) -> int:
    u = unit.lower()
    return n * (3600 if u.startswith(("h",)) else 1 if u.startswith("s") else 60)


@dataclass
class Command:
    kind: str                 # list_add, list_show, remind, volume, mute, sleep, ears, time, here
    args: dict = field(default_factory=dict)


SECTION_WORDS = {"shopping": "shopping", "grocery": "shopping", "groceries": "shopping",
                 "to do": "tasks", "todo": "tasks", "to-do": "tasks", "task": "tasks", "tasks": "tasks"}
ITEM = r"([a-z0-9][a-z0-9 ,'&/.-]{0,80}?)"

FAST: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), k) for p, k in [
    # lists
    (r"(?:add|put)\s+" + ITEM + r"\s+(?:to|on|onto)\s+(?:my|the)\s+"
     r"(?:(shopping|grocery|groceries|to do|todo|to-do|task|tasks)\s+)?list", "list_add"),
    (r"(?:make a note|take a note|note)(?:\s+that|:)?\s+" + ITEM, "note"),
    (r"(?:what's|what is|whats|what's on|read me|read|show me|show)\s+(?:on\s+)?(?:my|the)\s+"
     r"(?:(shopping|grocery|to do|todo|to-do)\s+)?list", "list_show"),
    # reminders
    (r"remind me in\s+" + NUM + r"\s+" + UNIT + r"\s+(?:to|about|that)\s+" + ITEM, "remind_in"),
    (r"remind me (?:to|about)\s+" + ITEM + r"\s+in\s+" + NUM + r"\s+" + UNIT, "remind_in_rev"),
    (r"remind me in half an hour\s+(?:to|about|that)\s+" + ITEM, "remind_half"),
    (r"remind me at\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s+(?:to|about|that)\s+" + ITEM, "remind_at"),
    (r"remind me (?:to|about)\s+" + ITEM + r"\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", "remind_at_rev"),
    # volume
    (r"(?:turn\s+(?:the\s+)?volume|volume|turn it|turn yourself)\s+(up|down)", "volume_step"),
    (r"(?:speak\s+|talk\s+|be\s+)?(?:a (?:bit|little)\s+)?(louder|quieter|softer)", "volume_word"),
    (r"(?:set\s+(?:the\s+|your\s+)?)?volume\s+(?:to\s+)?(\d{1,3})(?:\s*(?:percent|%))?", "volume_set"),
    # mute / sleep
    (r"mute(?:\s+(?:yourself|the mic|your mic|the microphone|your microphone))?", "mute"),
    (r"go to sleep|time to sleep|go to bed|sleep now|take a nap", "sleep"),
    # ears
    (r"(?:use|switch to|turn on)\s+(local|cloud|openai)\s+ears|ears\s+(local|cloud|openai)", "ears"),
    # time
    (r"what time is it|what's the time|whats the time|what is the time|tell me the time|"
     r"do you have the time", "time"),
    # presence
    (PRESENCE_CHECK.pattern.strip("^$"), "here"),
    # inbox (things said in passing, sorted later by the review)
    (r"(?:add|put)\s+" + ITEM + r"\s+(?:to|in|into|on)\s+(?:my|the)\s+inbox", "inbox_add"),
    # focus mode: time boxes (the conductor only takes done / more / park while a box exists)
    (r"(?:let's\s+|lets\s+|i want to\s+|i'm going to\s+|i'll\s+|time to\s+)?focus\s+on\s+" + ITEM
     + r"\s+for\s+(?:the next\s+)?(?:" + NUM + r"[\s-]+" + UNIT + r"|(half an hour))", "focus_on"),
    (r"(?:start|set|begin|do|give me)\s+(?:a|an|another)?\s*(?:" + NUM + r"[\s-]+" + UNIT + r"|(half an hour|half hour))"
     r"\s+(?:focus\s+)?(?:box|timer|block|session|sprint|focus box|focus session)?\s*(?:on|for)\s+" + ITEM, "focus_box"),
    (r"(?:start|set|begin)\s+(?:a|an)\s+(?:focus\s+)?(?:box|block|session)\s+(?:on|for)\s+" + ITEM, "focus_box_default"),
    (NUM + r"\s+" + UNIT + r"\s+on\s+" + ITEM, "focus_short"),
    (r"(?:i'm|i am|im)\s+(?:all\s+)?(?:done|finished)(?:\s+with\s+(?:it|that|this|the box|the task))?|"
     r"(?:all\s+)?done|finished|i(?:'ve)? finished(?:\s+it)?|finished it|mark it (?:as )?done|box done|"
     r"(?:that's|that is|it's|it is)\s+done", "focus_done"),
    (r"(?:give me\s+|i need\s+|i'll need\s+|can i (?:get|have)\s+|let's do\s+|let me have\s+)?(?:another\s+|an extra\s+)?"
     + NUM + r"\s+(?:more\s+)?(?:minutes?|mins?)(?:\s+more)?", "focus_more"),
    (r"(?:give me\s+|i need\s+|can i (?:get|have)\s+)?(?:a bit\s+|a little\s+|some\s+)?more time|"
     r"(?:another|one more)\s+(?:box|round)|keep going|extend (?:it|the box|the timer)", "focus_more_default"),
    (r"(?:let's\s+|lets\s+)?park\s+(?:it|that|this|the box)(?:\s+for later)?", "focus_park"),
    (r"(?:stop|end|cancel|finish)\s+(?:the\s+|my\s+|this\s+)?(?:focus\s+)?(?:box|timer|focus(?:\s+mode)?|time box)", "focus_stop"),
    (r"what's next|what is next|whats next|what's up next|what is up next|what's on next|"
     r"what should i (?:do|work on|be doing)(?: next| now)?|what am i (?:doing|working on|supposed to be doing)(?: now)?|"
     r"what's my next (?:thing|task)|how much time (?:is |have i got |do i have )?left|how long (?:is |have i got |do i have )?left|"
     r"how long have i got", "focus_next"),
]]


def _clock(h: int, m: int, ampm: str | None, now: dt.datetime) -> dt.datetime:
    """Next future time for 'at 5', 'at 5:30 pm' (no am/pm: the nearer future one)."""
    if not 0 <= m < 60 or not 0 <= h <= 23:
        raise ValueError("bad time")
    cands = []
    if ampm:
        pm = ampm.lower().startswith("p")
        if h > 12:
            raise ValueError("bad time")
        hh = (h % 12) + (12 if pm else 0)
        cands = [hh]
    elif h > 12:
        cands = [h]
    else:
        cands = [h % 12, (h % 12) + 12]
    best = None
    for hh in cands:
        t = now.replace(hour=hh, minute=m, second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
        if best is None or t < best:
            best = t
    return best


def _task(text: str) -> str:
    t = text.strip(" ,.!?")
    return t[:1].upper() + t[1:] if t else t


def match(text: str, wake_re: re.Pattern | None = None, now: dt.datetime | None = None) -> Command | None:
    """A fast-path command if the WHOLE utterance is one exact pattern; else None
    (the brain handles it). Never guesses."""
    if "\n" in text or text.count("?") > 1 or len(re.findall(r"[.!?](?:\s|$)", text.strip())) > 1:
        return None   # more than one sentence: let the brain sort it out
    t = strip_name(text, wake_re)
    if not t or len(t) > 140 or re.search(r"\b(and then|and also|after that|but|unless|if)\b", t, re.I):
        return None
    now = now or dt.datetime.now()
    for rx, kind in FAST:
        m = rx.fullmatch(t)
        if not m:
            continue
        g = m.groups()
        try:
            if kind == "list_add":
                item = g[0].strip()
                if re.search(r"\b(it|that|this|them|those)\b", item, re.I) and len(words(item)) <= 2:
                    return None   # "add it to my list": what's "it"? the brain knows
                return Command("list_add", {"text": _task(item),
                                            "section": SECTION_WORDS.get((g[1] or "").lower(), "tasks")
                                            if g[1] else ("tasks")})
            if kind == "note":
                if len(words(g[0])) < 2:
                    return None
                return Command("list_add", {"text": _task(g[0]), "section": "notes"})
            if kind == "list_show":
                sec = SECTION_WORDS.get((g[0] or "").lower()) if g[0] else None
                return Command("list_show", {"section": sec})
            if kind in ("remind_in", "remind_in_rev", "remind_half"):
                if kind == "remind_half":
                    secs, what = 1800, g[0]
                elif kind == "remind_in":
                    n = _num(g[0])
                    if n is None:
                        return None
                    secs, what = _secs(n, g[1]), g[2]
                else:
                    n = _num(g[1])
                    if n is None:
                        return None
                    secs, what = _secs(n, g[2]), g[0]
                if not 10 <= secs <= 24 * 3600 or not what.strip():
                    return None
                return Command("remind", {"due": now.timestamp() + secs, "text": _task(what),
                                          "in_s": secs})
            if kind in ("remind_at", "remind_at_rev"):
                if kind == "remind_at":
                    h, mi, ap, what = g
                else:
                    what, h, mi, ap = g
                when = _clock(int(h), int(mi or 0), ap, now)
                if not what.strip():
                    return None
                return Command("remind", {"due": when.timestamp(), "text": _task(what),
                                          "at": when.strftime("%-I:%M %p")})
            if kind == "volume_step":
                return Command("volume", {"args": [g[0].lower()]})
            if kind == "volume_word":
                return Command("volume", {"args": ["up" if g[0].lower() == "louder" else "down"]})
            if kind == "volume_set":
                v = int(g[0])
                if v > 100:
                    return None
                return Command("volume", {"args": ["set", str(v)]})
            if kind in ("mute", "sleep", "time", "here"):
                return Command(kind)
            if kind == "inbox_add":
                if re.search(r"\b(it|that|this|them|those)\b", g[0], re.I) and len(words(g[0])) <= 2:
                    return None
                return Command("list_add", {"text": _task(g[0]), "section": "inbox"})
            if kind in ("focus_on", "focus_box", "focus_short", "focus_box_default"):
                if kind == "focus_on":
                    what, n, unit, half = g
                elif kind == "focus_box_default":
                    what, n, unit, half = g[0], "25", "minutes", None
                elif kind == "focus_box":
                    n, unit, half, what = g
                else:
                    n, unit, what = g
                    half = None
                mins = 30 if half else (_secs(_num(n), unit) / 60 if _num(n) else None)
                what = re.sub(r"^(?:the|my)\s+", "", (what or "").strip(), flags=re.I)
                if not mins or not 1 <= mins <= 240 or not what or len(words(what)) > 10:
                    return None
                if kind == "focus_short" and mins < 5:
                    return None   # "two minutes on the eggs": a kitchen timer, not a box
                return Command("focus_start", {"task": _task(what), "minutes": mins})
            if kind == "focus_done":
                return Command("focus_done")
            if kind == "focus_more":
                n = _num(g[0])
                if not n or not 1 <= n <= 120:
                    return None
                return Command("focus_more", {"minutes": n})
            if kind == "focus_more_default":
                return Command("focus_more", {"minutes": 10, "default": True})
            if kind in ("focus_park", "focus_stop", "focus_next"):
                return Command(kind)
            if kind == "ears":
                which = (g[0] or g[1]).lower()
                return Command("ears", {"provider": "local" if which == "local" else "openai"})
        except (ValueError, TypeError):
            return None
    return None


# ------------------------------------------------------------ replies
REPLIES = {
    "list_add": ["Added {text}.", "Done, {text} is on the list.", "Got it: {text}.",
                 "{text}, on the list."],
    "note": ["Noted.", "Got it, it's in your notes.", "Written down."],
    "remind": ["I'll remind you {when}.", "Okay, {when}. I'll tell you.", "Reminder set for {when}."],
    "volume": ["[happy] How's that?", "There you go.", "Better?"],
    "time": ["It's {time}.", "{time}.", "It's about {time}."],
    "here": ["[happy] I'm here.", "Yep, right here.", "Loud and clear."],
    "here_busy": ["I'm here, still working on it.", "Still here, just thinking.",
                  "Yep, I heard you. Still working on that."],
    "ears": ["Okay, {which} ears now.", "Switched to {which} ears."],
    "empty": ["Your list's empty.", "Nothing on the list right now."],
    "inbox_add": ["In the inbox: {text}.", "Got it, {text} is in the inbox.", "Inbox: {text}."],
    "focus_start": ["[happy] {mins} on {task}. Go get it.", "Right then, {mins} on {task}. I'll keep things quiet.",
                    "Box started: {task}, {mins}. I'll hold anything that can wait.",
                    "{mins} on {task}. I'll give you a shout at five to go."],
    "focus_done": ["[happy] Nice, {task} done. {mins} well spent.", "Done and dusted: {task}, {mins}.",
                   "[proud] {task}, ticked off. That was {mins}."],
    "focus_more": ["{mins} more on {task}. Until {until}.", "Okay, another {mins}. You've got until {until}.",
                   "Extended: {task} until {until}."],
    "focus_park": ["Parked {task} for later. It's on your list.", "Okay, {task} is parked on the list.",
                   "Saved {task} for later."],
    "focus_stop": ["Box stopped.", "Okay, timer's off.", "Stopped the box on {task}."],
    "no_box": ["There's no focus box running.", "No box going right now."],
}


class Replies:
    def __init__(self):
        self.pickers = {k: Picker(v) for k, v in REPLIES.items()}

    def say(self, kind: str, **kw) -> str:
        return self.pickers[kind].pick().format(**kw)


def spoken_duration(secs: int) -> str:
    if secs < 60:
        return f"in {secs} seconds"
    if secs < 3600:
        m = round(secs / 60)
        return "in a minute" if m == 1 else f"in {m} minutes"
    h, m = divmod(round(secs / 60), 60)
    hours = "an hour" if h == 1 else f"{h} hours"
    return f"in {hours}" + (f" and {m} minutes" if m else "")


def spoken_list(items: list[str], limit: int = 5) -> str:
    if not items:
        return ""
    head = items[:limit]
    more = len(items) - len(head)
    if len(head) == 1:
        s = head[0]
    else:
        s = ", ".join(head[:-1]) + (", and " if more == 0 else ", ") + head[-1]
    if more:
        s += f", and {more} more"
    n = len(items)
    return f"You've got {n} thing{'s' if n != 1 else ''}: {s}."
