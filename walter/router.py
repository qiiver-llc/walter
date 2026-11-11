"""Which brain answers this turn: the fast model or the deep one.

Walter chats on a fast model (snappy small talk) and switches to a bigger
model when a turn is genuinely hard: reasoning, planning, code, research,
anything needing care. Switching is nearly free (Claude Code's `set_model`
control message, ~20 ms, same session), so the router decides per turn, with
a little hysteresis so a deep conversation doesn't flip-flop:

- explicit overrides in the words ("think hard", "use the big brain" /
  "quick answer", "just tell me") win;
- a local keyword/shape heuristic scores the heard text (score >= 2 = deep);
- after a deep turn the next STICK_TURNS easy turns stay deep (follow-ups),
  unless the person was quiet for IDLE_S;
- the fast model can hand a turn over itself by starting its reply with
  [think:hard] (the conductor discards that reply unspoken and re-asks).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("walter.router")

ESCALATE_TAG = "[think:hard]"
STICK_TURNS = 2        # easy turns that stay deep after a deep one
IDLE_S = 300.0         # ...unless this long passed since the last deep turn
DEEP_SCORE = 2
MODES = ("auto", "fast", "deep")
# Turns Walter starts himself: small, routine, the fast model is plenty.
BACKGROUND = {"heartbeat", "presence", "reminder", "routine", "task", "firstboot"}

DEEP_OVERRIDE_RE = re.compile(
    r"\b(think (really |very )?(hard|carefully|deeply|it through)|big(ger)? brain|deep brain|"
    r"use (the )?(opus|deep model|big model|smart model)|take your time|think about it properly)\b", re.I)
FAST_OVERRIDE_RE = re.compile(
    r"\b(quick (answer|one|question)|quickly|short answer|just tell me|in a word|"
    r"(use )?(the )?(small|little|fast) brain|use (sonnet|the fast model))\b", re.I)

# (pattern, weight, label). Strong signals are worth DEEP_SCORE on their own.
SIGNALS: list[tuple[re.Pattern, int, str]] = [(re.compile(p, re.I), w, lbl) for p, w, lbl in [
    (r"\bresearch\w*", 2, "research"),
    (r"\bcompar(e|es|ing|ison)\b|\bversus\b|\bvs\.?\s", 2, "compare"),
    (r"\bpros and cons\b|\btrade-?offs?\b", 2, "pros/cons"),
    (r"\bdebug\w*|\btroubleshoot\w*|\btraceback\b|\bstack ?trace\b|\bexception\b", 2, "debugging"),
    (r"\b(code|coding|script|function|refactor\w*|implement\w*|compile\w*|regex|sql|python|"
     r"javascript|typescript|api|repo|git|pull request|unit tests?)\b", 2, "code"),
    (r"\bplan(ning)?\b(?! on\b)|\bitinerary\b|\bstrategy\b|\bschedule out\b", 2, "planning"),
    (r"\banaly[sz]\w*|\bdesign\w*|\barchitect\w*", 2, "analysis/design"),
    (r"\bstep[- ]by[- ]step\b|\bwalk me through\b", 2, "step by step"),
    (r"\b(build|make) (me |us )?(a|an|the|my|some)\b", 1, "build"),
    (r"\bfix\w*|\bbroken\b|\bnot working\b|\bwon'?t (work|start|turn on)\b|\bbug\w*", 1, "fix"),
    (r"\bexplain\w*|\bwhy (is|are|do|does|did|would|should|can'?t|won'?t)\b", 1, "explain/why"),
    (r"\bhow (do|can|should|would) (i|we|you)\b|\bwhat'?s the best way\b|\bbest way to\b", 1, "how-to"),
    (r"\bwrite (me |us )?(a|an|the|my|some)\b|\bdraft\w*|\bessay\b|\bproposal\b", 1, "writing"),
    (r"\bhelp me (decide|figure|choose|work out|think|write|plan|understand)\b|\bfigure out\b|"
     r"\bwork out\b", 1, "help me think"),
    (r"\bsummari[sz]\w*|\breview\w*|\boptimi[sz]\w*|\bbudget\w*|\brecommend\w*", 1, "summarise/review"),
    (r"\bcalculat\w*|\bestimat\w*|\bhow (much|many|long) would\b", 1, "estimate"),
]]
MULTI_RE = re.compile(r"\b(and also|and then|after that|as well as|first\b.*\bthen)\b|;", re.I)


def classify(text: str) -> tuple[str, int, str]:
    """('deep' | 'fast' | 'neutral', score, reason) for the heard words alone.

    'fast' means an explicit request for a quick answer (it ends any deep
    streak); 'neutral' means nothing hard spotted (follow-up stickiness may
    still keep the deep model)."""
    t = (text or "").strip()
    if DEEP_OVERRIDE_RE.search(t):
        return "deep", 99, "asked to think hard"
    if FAST_OVERRIDE_RE.search(t):
        return "fast", 0, "asked for a quick answer"
    score, why = 0, []
    for rx, w, lbl in SIGNALS:
        if rx.search(t):
            score += w
            why.append(lbl)
    words = len(t.split())
    if words >= 60:
        score += 2
        why.append(f"long ({words} words)")
    elif words >= 30:
        score += 1
        why.append(f"longish ({words} words)")
    if t.count("?") >= 2 or MULTI_RE.search(t):
        score += 1
        why.append("multi-part")
    if score >= DEEP_SCORE:
        return "deep", score, ", ".join(why)
    if words <= 6 and not why:
        return "neutral", score, "small talk"
    return "neutral", score, (", ".join(why) + " (not enough)") if why else "simple question"


@dataclass
class Decision:
    deep: bool
    reason: str


class Router:
    """Picks fast vs deep per turn, with overrides and hysteresis."""

    def __init__(self, auto_switch: bool = True, state_file: Path | None = None,
                 stick_turns: int = STICK_TURNS, idle_s: float = IDLE_S):
        self.auto_switch = auto_switch
        self.state_file = state_file
        self.stick_turns = stick_turns
        self.idle_s = idle_s
        self.mode = "auto"           # walterctl brain fast|deep|auto
        self.last_deep_at: float | None = None  # last turn answered deep (None = no streak)
        self.easy_streak = 0         # easy turns since then that stayed deep
        self.last: Decision | None = None
        self._load()

    # -------------------------------------------------------------- override
    def _load(self):
        if not self.state_file:
            return
        try:
            m = json.loads(self.state_file.read_text()).get("mode")
            if m in MODES:
                self.mode = m
        except Exception:
            pass

    def set_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        if mode != "deep":
            self.last_deep_at, self.easy_streak = None, 0
        if self.state_file:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"mode": mode}))
            except OSError as e:
                log.warning("couldn't save brain mode: %s", e)

    # -------------------------------------------------------------- decide
    def _deep(self, now: float, reason: str) -> Decision:
        self.last_deep_at, self.easy_streak = now, 0
        return Decision(True, reason)

    def decide(self, text: str, source: str = "voice", now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        d = self._decide(text, source, now)
        self.last = d
        return d

    def _decide(self, text: str, source: str, now: float) -> Decision:
        if self.mode == "fast":
            return Decision(False, "forced fast (walterctl brain fast)")
        if self.mode == "deep":
            return Decision(True, "forced deep (walterctl brain deep)")
        if not self.auto_switch:
            return Decision(False, "auto switch off")
        if source in BACKGROUND:
            return Decision(False, f"{source} turn")
        kind, _score, reason = classify(text)
        if kind == "deep":
            return self._deep(now, reason)
        if kind == "fast":
            self.last_deep_at, self.easy_streak = None, 0
            return Decision(False, reason)
        if self.last_deep_at is not None:
            if now - self.last_deep_at > self.idle_s:
                self.last_deep_at, self.easy_streak = None, 0
                return Decision(False, f"{reason}; deep streak ended after "
                                       f"{int(self.idle_s // 60)} min quiet")
            if self.easy_streak < self.stick_turns:
                self.easy_streak += 1
                self.last_deep_at = now
                return Decision(True, f"follow-up to a deep turn ({self.easy_streak}/{self.stick_turns})")
            self.last_deep_at, self.easy_streak = None, 0
            return Decision(False, f"{reason}; back to fast after {self.stick_turns} easy turns")
        return Decision(False, reason)

    def escalated(self, now: float | None = None) -> Decision:
        """The fast model asked for the deep one with [think:hard]."""
        d = self._deep(time.time() if now is None else now, "fast model escalated with [think:hard]")
        self.last = d
        return d

    def status(self) -> dict:
        return {"mode": self.mode, "auto_switch": self.auto_switch,
                "last_decision": ({"deep": self.last.deep, "reason": self.last.reason}
                                  if self.last else None),
                "deep_streak": self.last_deep_at is not None}


class EscalationGate:
    """Holds back the start of a fast-model reply until we know whether it
    begins with [think:hard]. feed() returns text safe to pass on ('' while
    undecided); .escalate turns True if the tag was found (then nothing of
    this reply must be spoken)."""

    def __init__(self, enabled: bool = True):
        self.open = not enabled     # decided: pass everything through
        self.escalate = False
        self.buf = ""

    def feed(self, text: str) -> str:
        if self.open:
            return text
        if self.escalate:
            return ""
        self.buf += text
        head = self.buf.lstrip().lower()
        if head.startswith(ESCALATE_TAG):
            self.escalate = True
            return ""
        if not head or ESCALATE_TAG.startswith(head):
            return ""                   # still could be the tag
        self.open = True
        out, self.buf = self.buf, ""
        return out

    def flush(self) -> str:
        """End of reply (or a tool call): release whatever was held."""
        if self.escalate or not self.buf:
            return ""                   # (nothing held: a tool call doesn't decide it)
        self.open = True
        out, self.buf = self.buf, ""
        return out
