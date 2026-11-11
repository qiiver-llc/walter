"""Sleep-time learning: a light self-review while I'm asleep (dashboard mode).

When I've been properly asleep (the sleep button, quiet hours or the muted nap,
not just dozing in an empty room) for a few minutes, I hand one quiet,
low-priority background worker my recent logs (skills/self-review.sh) and ask it
to write lesson candidates to journal/ and propose backlog items marked [ask].

Kept cheap on purpose:
  - at most once per sleep, and not again within `sleep_review_gap_h` hours;
  - never while any worker is running (it would compete for a slot / the CPU);
  - never when weekly Claude usage (face/usage.json) is above a linear pace,
    unknown/stale, or at the 90 % stop line;
  - not around 3 AM, when the nightly self-review routine already does this;
  - the worker runs niced like every worker, on the fast model, and its report
    is NOT read out or pushed to the phone (it only writes notes).

The worker may only write under ~/.walter/home/journal and append [ask] lines
to backlog.md: no code, no pushes, no restarts, no messages, no spending.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("walter.sleeplearn")

TITLE = "Sleep-time review"
WEEK_S = 7 * 24 * 3600
STOP_PCT = 90.0          # never start new workers past this share of the weekly allowance
STALE_USAGE_S = 3 * 3600  # usage.json older than this counts as unknown


def usage_ok(usage: dict | None, now: float, margin: float = 5.0) -> tuple[bool, str]:
    """Is weekly usage at or under a linear pace through the week (+ margin points)?
    Unknown or stale usage counts as NOT ok: when in doubt, stay cheap."""
    if not isinstance(usage, dict) or not usage.get("ok"):
        return False, "usage unknown"
    upd = usage.get("updated")
    if isinstance(upd, (int, float)) and now - upd > STALE_USAGE_S:
        return False, "usage data stale"
    week = usage.get("week") or {}
    pct, resets = week.get("pct"), week.get("resets_at")
    if pct is None:
        return False, "usage unknown"
    pct = float(pct)
    if pct >= STOP_PCT:
        return False, f"usage {pct:.0f}% at the stop line"
    try:
        reset = dt.datetime.fromisoformat(str(resets).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return False, "usage reset time unknown"
    elapsed = min(1.0, max(0.0, 1.0 - (reset - now) / WEEK_S))
    pace = elapsed * 100.0
    if pct > pace + margin:
        return False, f"usage {pct:.0f}% above pace ({pace:.0f}%)"
    return True, f"usage {pct:.0f}% within pace ({pace:.0f}%)"


def decide(*, now: float, asleep_since: float | None, dozing: bool, state: dict,
           running: int, usage: dict | None, settle_min: float = 10.0,
           gap_h: float = 6.0, hour: int | None = None) -> tuple[bool, str]:
    """Pure decision: should a sleep-time review start now? -> (go, reason)."""
    if asleep_since is None or dozing:
        return False, "not asleep"
    if now - asleep_since < settle_min * 60:
        return False, "not settled yet"
    if state.get("sleep_id") == round(asleep_since):
        return False, "already reviewed this sleep"
    last = state.get("last_run") or 0
    if now - last < gap_h * 3600:
        return False, "reviewed recently"
    if hour is None:
        hour = dt.datetime.fromtimestamp(now).hour
    if 2 <= hour < 4:
        return False, "nightly self-review window"
    if running > 0:
        return False, "workers busy"
    ok, why = usage_ok(usage, now)
    if not ok:
        return False, why
    return True, why


def prompt(home: Path, now: float) -> str:
    """Instructions for the review worker (self-contained, deliberately narrow)."""
    day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    clock = dt.datetime.fromtimestamp(now).strftime("%I:%M %p").lstrip("0")
    journal = home / "journal"
    out = journal / f"{day}-sleep-review.md"
    return f"""This is my light SLEEP-TIME REVIEW (I'm asleep in dashboard mode). Be quick and cheap:
aim for a few minutes and a handful of tool calls, then stop.

1. Gather material: run `bash {home}/skills/self-review.sh {day} | tail -n 300`
   (today's ears log: heard / ignored / rejected / cut off, errors and warnings, and the conversation).
   Optionally skim the last ~30 lines of `{journal}/goal.md` and the newest journal entry, and
   `{home}/backlog.md` so you don't duplicate items.
2. Look for: errors or tracebacks, things I misheard or wrongly ignored, requests I never answered,
   replies that over-explained or repeated stock phrases, rough edges the owner complained about.
3. Write (append; create if missing) a section headed `## Sleep review, {clock}` to {out} with:
   - "What happened" (3 to 6 short bullets, facts with times),
   - "Lesson candidates" (0 to 3 one-line lessons; the 3 AM self-review decides which reach CLAUDE.md),
   - "Proposed fixes" (0 to 3).
4. For a proposed fix that is a real, new rough edge not already in backlog.md, append ONE line per
   fix (max 2) to the end of {home}/backlog.md in the format
   `- [ask] **Short title**: details (sleep review {day})`. Use [ask], never [ready].
   If nothing stands out, write "Quiet stretch, nothing new." and add no backlog lines.

Hard limits: write ONLY to {journal}/ and append to {home}/backlog.md. Do not edit code, CLAUDE.md,
memory.md or any other file. No git commits or pushes, no restarts, no `walterctl task`/`remind`/
`later`/`away`/`dash`/cards/canvas, no messages or email, no web browsing, no spending.
Never write family members' names, the town, or email addresses into backlog.md.
Final message: one plain sentence summarising what you noted (it is logged, not spoken)."""


class SleepLearner:
    """Called from the conductor's life loop every ~20 s; starts at most one quiet worker per sleep."""

    def __init__(self, cfg, tasks, state_path: Path, usage_path: Path):
        self.cfg = cfg
        self.tasks = tasks
        self.state_path = Path(state_path)
        self.usage_path = Path(usage_path)
        self.asleep_since: float | None = None
        self._starting = False
        self._last_reason = ""

    def _load(self, p: Path) -> dict:
        try:
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict):
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(self.state_path)
        except OSError as e:
            log.warning("sleep review: couldn't save state: %s", e)

    async def tick(self, *, sleeping: bool, dozing: bool, now: float | None = None,
                   hour: int | None = None) -> str:
        """Track the sleep episode; start the review if every guard passes. Returns the reason."""
        now = time.time() if now is None else now
        life = self.cfg.life
        if not getattr(life, "sleep_review", True) or self.tasks is None:
            return "off"
        if not sleeping:
            self.asleep_since = None
            return "awake"
        if self.asleep_since is None:
            self.asleep_since = now
        if self._starting:
            return "starting"
        state = self._load(self.state_path)
        go, why = decide(now=now, asleep_since=self.asleep_since, dozing=dozing, state=state,
                         running=self.tasks.running, usage=self._load(self.usage_path),
                         settle_min=getattr(life, "sleep_review_settle_min", 10.0),
                         gap_h=getattr(life, "sleep_review_gap_h", 6.0), hour=hour)
        if not go:
            if why != self._last_reason and why not in ("not settled yet", "already reviewed this sleep"):
                log.info("sleep review skipped: %s", why)
            self._last_reason = why
            return why
        self._starting = True
        state.update(sleep_id=round(self.asleep_since), last_run=now)
        self._save(state)   # saved first: a failure below must not retry every 20 s
        try:
            home = Path(self.cfg.brain.home)
            item = await self.tasks.start(TITLE, prompt(home, now), quiet=True,
                                          model=getattr(life, "sleep_review_model", "") or None)
            log.info("sleep review started as task #%d (%s)", item["id"], why)
            return "started"
        except Exception as e:
            log.warning("sleep review: couldn't start: %s", e)
            return "failed"
        finally:
            self._starting = False
