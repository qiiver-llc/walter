"""Sleep-time learning: guards (once per sleep, gap, workers busy, usage pace) and the tick."""
import asyncio
import datetime as dt
import json
from types import SimpleNamespace

from walter import sleeplearn as sl

NOW = 1_800_000_000.0   # fixed clock for the tests


def usage(pct, days_left, ok=True, updated=NOW):
    reset = dt.datetime.fromtimestamp(NOW + days_left * 86400, dt.timezone.utc).isoformat()
    return {"ok": ok, "week": {"pct": pct, "resets_at": reset}, "updated": updated}


def go(**kw):
    base = dict(now=NOW, asleep_since=NOW - 3600, dozing=False, state={}, running=0,
                usage=usage(10, 5), hour=23)
    base.update(kw)
    return sl.decide(**base)


def test_usage_pace():
    assert sl.usage_ok(usage(10, 5), NOW)[0]            # 2/7 through the week, pace ~29 %
    assert not sl.usage_ok(usage(60, 5), NOW)[0]        # well above pace
    assert not sl.usage_ok(usage(92, 0.1), NOW)[0]      # stop line even at the end of the week
    assert not sl.usage_ok({"ok": False}, NOW)[0]       # unknown = stay cheap
    assert not sl.usage_ok(usage(5, 5, updated=NOW - 5 * 3600), NOW)[0]   # stale
    assert not sl.usage_ok(None, NOW)[0]


def test_decide_guards():
    assert go()[0]
    assert go(asleep_since=None) == (False, "not asleep")
    assert go(dozing=True) == (False, "not asleep")
    assert go(asleep_since=NOW - 60) == (False, "not settled yet")
    assert go(state={"sleep_id": round(NOW - 3600)}) == (False, "already reviewed this sleep")
    assert go(state={"last_run": NOW - 3600}) == (False, "reviewed recently")
    assert go(running=1) == (False, "workers busy")
    assert go(hour=3) == (False, "nightly self-review window")
    assert not go(usage=usage(80, 5))[0]


def test_prompt_is_narrow(tmp_path):
    p = sl.prompt(tmp_path, NOW)
    assert "skills/self-review.sh" in p and "[ask]" in p
    assert "no restarts" in p and "pushes" in p and "journal" in p


class FakeTasks:
    def __init__(self, running=0):
        self.running = running
        self.started = []

    async def start(self, title, prompt, quiet=False, model=None):
        self.started.append((title, quiet, model))
        return {"id": len(self.started)}


def learner(tmp_path, tasks, pct=10):
    cfg = SimpleNamespace(life=SimpleNamespace(sleep_review=True, sleep_review_settle_min=10.0,
                                               sleep_review_gap_h=6.0, sleep_review_model="sonnet"),
                          brain=SimpleNamespace(home=str(tmp_path)))
    up = tmp_path / "usage.json"
    up.write_text(json.dumps(usage(pct, 5)))
    return sl.SleepLearner(cfg, tasks, tmp_path / "state.json", up)


def run(c):
    return asyncio.run(c)


def test_tick_runs_once_per_sleep(tmp_path, monkeypatch):
    t = FakeTasks()
    l = learner(tmp_path, t)
    assert run(l.tick(sleeping=True, dozing=False, hour=23, now=NOW)) == "not settled yet"
    assert run(l.tick(sleeping=True, dozing=False, hour=23, now=NOW + 700)) == "started"
    assert t.started == [(sl.TITLE, True, "sonnet")]
    assert run(l.tick(sleeping=True, dozing=False, hour=23, now=NOW + 800)) == "already reviewed this sleep"
    # wake up, sleep again soon after: the gap stops a second run
    run(l.tick(sleeping=False, dozing=False, hour=23, now=NOW + 900))
    assert run(l.tick(sleeping=True, dozing=False, hour=23, now=NOW + 1000)) == "not settled yet"
    assert run(l.tick(sleeping=True, dozing=False, hour=23, now=NOW + 2000)) == "reviewed recently"
    assert len(t.started) == 1


def test_tick_skips_when_busy_or_over_pace(tmp_path, monkeypatch):
    busy = learner(tmp_path, FakeTasks(running=1))
    busy.asleep_since = NOW - 3600
    assert run(busy.tick(sleeping=True, dozing=False, hour=23, now=NOW)) == "workers busy"
    over = learner(tmp_path, FakeTasks(), pct=70)
    over.asleep_since = NOW - 3600
    assert "above pace" in run(over.tick(sleeping=True, dozing=False, hour=23, now=NOW))
    assert not (tmp_path / "state.json").exists()   # nothing recorded when skipped


def test_tick_off(tmp_path):
    l = learner(tmp_path, FakeTasks())
    l.cfg.life.sleep_review = False
    assert run(l.tick(sleeping=True, dozing=False, hour=23, now=NOW)) == "off"

