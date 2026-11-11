"""Sports feed for the portrait dashboard (face/dashboard.js .sp, above the 7-day strip).

For each followed team ([dashboard] sports_teams in ~/.walter/config.toml, entries like
"nfl/gb" or "mlb/nyy") we ask ESPN's public site API (no key):
  .../sports/<sport>/<league>/teams/<team>            small; nextEvent (also the live game)
  .../sports/<sport>/<league>/teams/<team>/schedule   the season: last result + next game
and write face/dashboard-sports.json with, per team, the live game (if any), the last
finished game, the next one, and `games`: every game from GAMES_BACK_S ago to GAMES_AHEAD_S
ahead (results + upcoming), which the portrait month calendar puts on their days.
The face picks what to show for the current time.

Refreshed by bin/walter-dashboard refresh (walter-dashboard.timer, 15 min). While a
followed game is live (or about to start) walter-sports.timer runs
`walter-dashboard sports --live-only` every 2 minutes; otherwise that is a no-op that
makes no network call. Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.request
from pathlib import Path

from walter import dashboard as D


def local_tz_name() -> str:
    """This computer's IANA time zone ('Europe/London'), or "" if it can't be told."""
    import os
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            name = Path("/etc/timezone").read_text().strip()
        except OSError:
            real = os.path.realpath("/etc/localtime")
            name = real.split("zoneinfo/", 1)[1] if "zoneinfo/" in real else ""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return name
    except Exception:
        return ""


def local_zone():
    """tzinfo for local_tz_name(), else the system's current offset."""
    name = local_tz_name()
    if name:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    return dt.datetime.now().astimezone().tzinfo

API = "https://site.api.espn.com/apis/site/v2/sports"
# league slug -> ESPN sport path
SPORT = {
    "college-football": "football", "nfl": "football", "ufl": "football",
    "nba": "basketball", "wnba": "basketball", "mens-college-basketball": "basketball",
    "womens-college-basketball": "basketball", "mlb": "baseball", "college-baseball": "baseball",
    "nhl": "hockey", "mens-college-hockey": "hockey", "mls": "soccer", "usa.1": "soccer",
    "college-volleyball": "volleyball", "womens-college-volleyball": "volleyball",
}
DEFAULT_TEAMS: list[str] = []   # none by default; set [dashboard] sports_teams in ~/.walter/config.toml
LIVE_EARLY_S = 15 * 60        # start live polling this long before kick-off
LIVE_MAX_S = 6 * 3600         # ...and give up this long after it (a game stuck "in")
GAMES_BACK_S = 38 * 86400     # the month calendar starts on the Sunday on/before the 1st
GAMES_AHEAD_S = 43 * 86400    # ...and ends on the Saturday on/after the month's last day
MAX_GAMES = 24


def out_path() -> Path:
    return D.FACE_DIR / "dashboard-sports.json"


def parse_team(spec) -> tuple[str, str, str] | None:
    """'college-football/9001' | 'football/nfl/gb' | 'nfl:gb' -> (sport, league, team)."""
    parts = [p for p in re.split(r"[/:\s]+", str(spec or "").strip().lower()) if p]
    if len(parts) == 2 and parts[0] in SPORT:
        return SPORT[parts[0]], parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return None


def _get(url: str, timeout: float = 12):
    req = urllib.request.Request(url, headers={"User-Agent": D.USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _score(c):
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("displayValue", s.get("value"))
    if s in (None, ""):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _clock(st: dict, sport: str) -> str:
    t = st.get("type") or {}
    name = (t.get("name") or "").upper()
    if "HALFTIME" in name:
        return "Half"
    if "END_PERIOD" in name or "END_OF_PERIOD" in name:
        p = int(st.get("period") or 0)
        return f"End Q{p}" if sport == "football" and 0 < p <= 4 else D.clean_text(t.get("shortDetail"), 20)
    if sport == "football":
        p = int(st.get("period") or 0)
        clk = D.clean_text(st.get("displayClock"), 8)
        return (f"Q{p}" if p <= 4 else "OT") + (f" {clk}" if clk else "") if p else D.clean_text(t.get("shortDetail"), 20)
    return D.clean_text(t.get("shortDetail") or t.get("detail"), 20)


def parse_event(ev: dict, team_id: str, sport: str) -> dict | None:
    """One ESPN event -> {id, start, state, us, them, home, detail, tbd, result}."""
    try:
        comp = (ev.get("competitions") or [])[0]
        cs = comp.get("competitors") or []
        us = next(c for c in cs if str(c.get("id") or (c.get("team") or {}).get("id")) == str(team_id))
        them = next(c for c in cs if c is not us)
    except (IndexError, StopIteration, AttributeError):
        return None
    st = comp.get("status") or ev.get("status") or {}
    t = st.get("type") or {}
    state = t.get("state") or "pre"
    side = lambda c: {"abbr": D.clean_text((c.get("team") or {}).get("abbreviation"), 6).upper() or "?",
                      "score": _score(c)}
    out = {"id": str(ev.get("id") or ""), "start": ev.get("date") or comp.get("date") or "",
           "state": state, "us": side(us), "them": side(them),
           "home": us.get("homeAway") == "home",
           "tbd": comp.get("timeValid") is False or bool(st.get("isTBDFlex"))}
    if state == "in":
        out["detail"] = _clock(st, sport)
    elif state == "post":
        if not t.get("completed"):          # postponed / cancelled: not a result
            out["state"] = "off"
            out["detail"] = D.clean_text(t.get("description") or "Postponed", 20)
        else:
            out["detail"] = D.clean_text(t.get("shortDetail") or "Final", 20)
            a, b = out["us"]["score"], out["them"]["score"]
            w = us.get("winner")
            out["result"] = "W" if w is True or (w is None and a is not None and b is not None and a > b) else \
                "T" if a is not None and a == b else "L"
    return out


def _ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def compact(g: dict) -> dict:
    """A game for the `games` list: only what a calendar chip needs."""
    out = {k: g[k] for k in ("id", "start", "state", "home", "tbd", "result", "detail") if g.get(k) not in (None, "")}
    out["home"] = bool(g.get("home"))
    out["us"] = dict(g.get("us") or {})
    out["them"] = dict(g.get("them") or {})
    return out


def games_window(evs, now: float) -> list[dict]:
    """Games from GAMES_BACK_S ago to GAMES_AHEAD_S ahead, compact, by start."""
    keep = [e for e in evs if e.get("start") and now - GAMES_BACK_S <= _ts(e["start"]) <= now + GAMES_AHEAD_S]
    keep.sort(key=lambda e: _ts(e["start"]))
    return [compact(e) for e in keep[:MAX_GAMES]]


def merge_games(old: list | None, fresh: list, now: float) -> list[dict]:
    """The live-only pass has no schedule: keep the stored games, updated with the fresh ones (by id)."""
    by = {}
    for g in (old or []) + [compact(g) for g in fresh if g]:
        by[g.get("id") or g.get("start")] = g
    return games_window(list(by.values()), now)


def summarize(team_json: dict | None, sched_json: dict | None, sport: str, league: str, now: float | None = None) -> dict:
    """Both ESPN documents for one team -> the face's entry (live / last / next)."""
    now = now or time.time()
    team = (team_json or {}).get("team") or (sched_json or {}).get("team") or {}
    tid = str(team.get("id") or "")
    events = {}
    for ev in ((sched_json or {}).get("events") or []) + (team.get("nextEvent") or []):
        e = parse_event(ev, tid, sport)
        if e and e["start"]:
            events[e["id"] or e["start"]] = e   # nextEvent (the fresher one) wins
    evs = sorted(events.values(), key=lambda e: _ts(e["start"]))
    color = D.clean_text(team.get("color"), 6).lower()
    rec = ""
    try:
        rec = D.clean_text(((team.get("record") or {}).get("items") or [{}])[0].get("summary"), 12)
    except (AttributeError, IndexError):
        pass
    entry = {"league": league, "abbr": D.clean_text(team.get("abbreviation"), 6).upper(),
             "name": D.clean_text(team.get("shortDisplayName") or team.get("name"), 30),
             "color": color if re.fullmatch(r"[0-9a-f]{6}", color or "") else "", "record": rec,
             "live": None, "last": None, "next": None}
    live = [e for e in evs if e["state"] == "in" and now - _ts(e["start"]) < LIVE_MAX_S]
    if live:
        entry["live"] = live[-1]
    done = [e for e in evs if e["state"] == "post" and _ts(e["start"]) <= now]
    if done:
        entry["last"] = done[-1]
    nxt = [e for e in evs if e["state"] == "pre" and _ts(e["start"]) > now - LIVE_MAX_S]
    if nxt:
        entry["next"] = nxt[0]
    entry["games"] = games_window(evs, now)
    return entry


def fetch_team(spec, now: float | None = None, get=_get, schedule: bool = True) -> dict | None:
    p = parse_team(spec)
    if not p:
        return None
    sport, league, team = p
    base = f"{API}/{sport}/{league}/teams/{team}"
    tj = get(base)
    sj = get(base + "/schedule") if schedule else None
    e = summarize(tj, sj, sport, league, now)
    e["spec"] = f"{league}/{team}"
    return e


def live_window(data: dict, now: float | None = None) -> bool:
    """True while a followed game is live, or kicks off within LIVE_EARLY_S."""
    now = now or time.time()
    for t in data.get("teams") or []:
        if t.get("src") == "nsaa":                 # high school: no live scores to poll for
            continue
        if t.get("live"):
            return True
        n = t.get("next") or {}
        s = _ts(n.get("start", ""))
        if s and not n.get("tbd") and s - LIVE_EARLY_S <= now < s + LIVE_MAX_S:
            return True
    return False


def refresh(specs, live_only: bool = False, now: float | None = None, get=_get, out: Path | None = None,
            rows: int = 2, hs=None, hs_fetcher=None) -> dict:
    """Fetch every team and write the face file. live_only: do nothing (no network) unless a
    game is live or about to start, and then only the small team endpoint per team.
    A failed team keeps its last good entry; if every team fails the old data stays and
    ok=false (the face hides the row once the data is old).
    hs: [dashboard] hs_teams (walter/hs_sports.py, NSAA pages, cached 30 min on disk); never
    fetched in the live-only pass, dropped out of season."""
    out = Path(out) if out else out_path()
    try:
        prev = json.loads(out.read_text())
    except (OSError, ValueError):
        prev = {}
    specs = [s for s in (specs if specs is not None else DEFAULT_TEAMS) if parse_team(s)]
    from walter import hs_sports as HS
    hcfg = [c for c in (HS.parse_cfg(x) for x in (hs or [])) if c]
    if live_only and not live_window(prev, now):
        return {"skipped": True, **prev}
    old = {t.get("spec"): t for t in prev.get("teams") or []}
    teams, errors = [], []
    for s in specs:
        p = parse_team(s)
        key = f"{p[1]}/{p[2]}"
        if live_only and key in old and not live_window({"teams": [old[key]]}, now):
            teams.append(old[key])                 # not this team's game: no call
            continue
        try:
            e = fetch_team(s, now, get, schedule=not live_only)
            o = old.get(key) or {}
            if live_only and o.get("live") and not e.get("live"):
                e = fetch_team(s, now, get)        # the game just ended: get the result
            elif live_only:                        # the small endpoint has no history
                if o.get("last") and not (e.get("last") and _ts(e["last"]["start"]) >= _ts(o["last"]["start"])):
                    e["last"] = o["last"]
                gone = {(g or {}).get("id") for g in (e.get("live"), e.get("last"))}
                if not e.get("next") and o.get("next") and o["next"].get("id") not in gone:
                    e["next"] = o["next"]
                e["games"] = merge_games(o.get("games"), [e.get("live"), e.get("next")],
                                         now or time.time())
            teams.append(e)
        except Exception as ex:  # offline, ESPN changed, ...
            errors.append(f"{key}: {str(ex)[:80]}")
            if key in old:
                teams.append(old[key])
    fx = hs_fetcher or HS.Fetcher(now)
    for c in hcfg:
        key = c["spec"]
        if live_only:
            key in old and teams.append(old[key])
            continue
        try:
            e = HS.fetch_team(c, fx)
            e and teams.append(e)
        except Exception as ex:  # offline, the NSAA changed its pages, ...
            errors.append(f"{key}: {str(ex)[:80]}")
            if key in old and HS.in_season(c["sport"], fx.now):
                teams.append(old[key])
    data = {"teams": teams, "tz": local_tz_name(), "rows": max(1, min(4, int(rows or 2)))}
    if errors and len(errors) == len(specs) + len(hcfg):
        data = {**prev, "ok": False, "error": "; ".join(errors)[:300], "failed_at": time.time()}
        if not prev:
            data["teams"] = []
    else:
        data.update({"ok": True, "updated": time.time()})
        if errors:
            data["error"] = "; ".join(errors)[:300]
    data["live"] = live_window(data, now)
    D.write_face_json(out, data)
    return data


def _local(iso: str, tbd=False) -> str:
    """'2026-11-07T01:00Z' -> 'Fri 6 Nov 7:00 PM' (this computer's time zone, 12-hour)."""
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(local_zone())
    except (ValueError, AttributeError):
        return iso
    return d.strftime("%a %-d %b") + (" TBD" if tbd else " " + d.strftime("%-I:%M %p"))


def describe(data: dict) -> str:
    """One line per team, for the CLI."""
    if not data.get("teams"):
        return "sports: nothing yet" + (f" ({data['error']})" if data.get("error") else "")
    lines = []
    for t in data["teams"]:
        bits = [(t.get("abbr") or t.get("spec") or "?") + (" " + t["tag"] if t.get("tag") else "")]
        if t.get("live"):
            g = t["live"]
            bits.append(f"LIVE {g['us']['score']}-{g['them']['score']} {g['them']['abbr']} {g.get('detail', '')}")
        if t.get("last"):
            g = t["last"]
            bits.append(f"last {g.get('result', '')} {g['us']['score']}-{g['them']['score']} "
                        f"{'vs' if g['home'] else 'at'} {g['them']['abbr']} ({_local(g['start'], True)[:-4]})")
        if t.get("next"):
            g = t["next"]
            bits.append(f"next {_local(g['start'], g.get('tbd'))} {'vs' if g['home'] else 'at'} {g['them']['abbr']}")
        lines.append("sports: " + ", ".join(bits))
    return "\n".join(lines)
