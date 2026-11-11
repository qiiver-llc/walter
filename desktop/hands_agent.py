"""The laptop side of hands (docs/HANDS.md): fetch actions the brain asks for, decide
run / ask / refuse with walter.hands.Gate, get a person's yes when needed (spoken at
the wall, or tapped on this laptop's own Allow / Deny dialog), run it, send the result.

Kill switch, any of: the tray's "Stop hands", `walter-desktop hands stop` (or the
hotkey, Ctrl+Alt+Shift+H by default, when pynput is installed) on this laptop, or
`walterctl hands stop` on the wall. The local stop is a file (hands.stop in the
config folder) so it survives restarts until `walter-desktop hands start`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from walter import hands as H

log = logging.getLogger("walter.hands_agent")
APPROVAL_TIMEOUT_S = 120
POLL_S = 25


def stop_file(cfg_dir: Path) -> Path:
    return Path(cfg_dir) / "hands.stop"


# ------------------------------------------------------------------ the laptop's own Allow / Deny
def prompt_argv(osn: str, which, title: str, text: str, seconds: int) -> list[str] | None:
    if osn == "linux":
        if which("zenity"):
            return ["zenity", "--question", f"--title={title}", f"--text={text}", "--ok-label=Allow",
                    "--cancel-label=Deny", f"--timeout={seconds}", "--no-markup"]
        if which("kdialog"):
            return ["kdialog", "--title", title, "--yes-label", "Allow", "--no-label", "Deny", "--yesno", text]
        return None
    if osn == "mac":
        # text and title arrive as argv items, never pasted into the script
        return ["osascript", "-e", "on run argv", "-e",
                "display dialog (item 2 of argv) with title (item 1 of argv) buttons {\"Deny\", \"Allow\"} "
                f"default button \"Deny\" giving up after {int(seconds)}", "-e",
                "return button returned of result", "-e", "end run", title, text]
    if osn == "windows":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Add-Type -AssemblyName PresentationFramework; "
                "$r=[System.Windows.MessageBox]::Show($env:WALTER_HANDS_TEXT,$env:WALTER_HANDS_TITLE,'YesNo','Warning');"
                "if($r -eq 'Yes'){exit 0}else{exit 1}"]
    return None


class LocalPrompt:
    """An Allow / Deny dialog on the laptop (zenity, kdialog, osascript, PowerShell).
    answer() blocks: True (tapped Allow), False (Deny), None (closed, timed out, cancelled)."""

    def __init__(self, osn: str, which=shutil.which, spawn=subprocess.Popen):
        self.osn, self.which, self.spawn = osn, which, spawn
        self.proc = None

    def available(self) -> bool:
        return prompt_argv(self.osn, self.which, "x", "x", 1) is not None

    def answer(self, title: str, text: str, seconds: int = APPROVAL_TIMEOUT_S) -> bool | None:
        argv = prompt_argv(self.osn, self.which, title, text, seconds)
        if not argv:
            return None
        env = dict(os.environ, WALTER_HANDS_TEXT=text, WALTER_HANDS_TITLE=title)
        try:
            self.proc = self.spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
            out, _ = self.proc.communicate(timeout=seconds + 5)
        except Exception:
            self.cancel()
            return None
        code = self.proc.returncode
        self.proc = None
        if self.osn == "mac":
            return True if (out or "").strip() == "Allow" else (False if (out or "").strip() == "Deny" else None)
        if code == 0:
            return True
        return False if code == 1 else None

    def cancel(self):
        p = self.proc
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
        self.proc = None


# ------------------------------------------------------------------ the agent
class HandsAgent:
    def __init__(self, http, cfg_dir: Path, osn: str, policy: H.Policy | None = None,
                 hands: H.Hands | None = None, prompt: LocalPrompt | None = None, clock=time.time,
                 sleep=time.sleep):
        self.http = http
        self.cfg_dir = Path(cfg_dir)
        self.osn = osn
        self.policy_path = self.cfg_dir / "hands.json"
        self.policy = policy or H.load_policy(self.policy_path)
        self.gate = H.Gate(self.policy, clock)
        self.hands = hands or H.Hands(osn, self.policy)
        self.prompt = prompt if prompt is not None else (LocalPrompt(osn) if self.policy.local_prompt else None)
        self.audit_path = self.cfg_dir / "hands-audit.log"
        self.clock, self.sleep = clock, sleep
        self.running = True
        self.remote_stop = False

    # ---- kill switch
    def locally_stopped(self) -> bool:
        return stop_file(self.cfg_dir).exists()

    def stopped(self) -> bool:
        return self.locally_stopped() or self.remote_stop

    def kill(self, why: str = "kill switch"):
        """Halt everything now: pending yeses are dropped, any dialog is closed, temp files wiped."""
        stop_file(self.cfg_dir).touch()
        self.gate.kill()
        if self.prompt is not None:
            self.prompt.cancel()
        self.hands.wipe()
        H.audit(self.audit_path, "stopped", reason=why)
        try:
            self.http.json("POST", "/api/face/hands/hello", self.caps())
        except Exception:
            pass

    def caps(self) -> dict:
        tools = {k: v for k, v in self.hands.tools.items() if v}
        return {"enabled": self.policy.enabled, "stopped": self.locally_stopped(), "tools": tools,
                "folders": len(self.policy.allowed_folders), "apps": sorted(self.policy.apps),
                "run_command": self.policy.run_command, "os": self.osn,
                "local_prompt": bool(self.prompt and self.prompt.available())}

    # ---- one request
    def handle(self, req: dict) -> dict:
        rid, action, params = str(req.get("id") or ""), str(req.get("action") or ""), req.get("params") or {}
        owner = req.get("owner_present")
        self.policy = H.load_policy(self.policy_path) if self.policy_path.exists() else self.policy
        self.gate.policy = self.hands.policy = self.policy
        if self.stopped():
            self.gate.kill()
        else:
            self.gate.revive()
        ctx = self.hands.context(owner if isinstance(owner, bool) else None)
        v = self.gate.decide(action, params, ctx)
        H.audit(self.audit_path, "asked", id=rid, action=action, params=params, risk=v.risk, decision=v.decision,
                reason=v.reason)
        if v.decision == H.REFUSE:
            return self._result(rid, v, {"ok": False, "refused": True, "error": v.reason})
        if v.decision == "ask":
            how = self.get_yes(rid, v)
            if not how:
                return self._result(rid, v, {"ok": False, "refused": True, "error": "not approved"})
            if v.risk == H.HIGH:   # re-check: things may have changed while we waited
                ctx = self.hands.context(owner if isinstance(owner, bool) else None)
                if ctx.locked or ctx.away(self.policy) or self.stopped():
                    return self._result(rid, v, {"ok": False, "refused": True,
                                                 "error": "the laptop got locked or left while waiting"})
            if not self.gate.grant(v, how):
                return self._result(rid, v, {"ok": False, "refused": True, "error": "that needs a person's yes"})
            if v.risk == H.MEDIUM:
                H.save_policy(self.policy, self.policy_path)   # a newly approved site is kept
        if self.stopped():
            return self._result(rid, v, {"ok": False, "refused": True, "error": "hands were stopped"})
        try:
            res = self.hands.do(action, params)
            if res.pop("policy_changed", False):
                H.save_policy(self.policy, self.policy_path)
        except H.Refused as e:
            res = {"ok": False, "refused": True, "error": str(e)}
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        return self._result(rid, v, res)

    def _result(self, rid: str, v: H.Verdict, res: dict) -> dict:
        res = dict(res, id=rid, risk=v.risk, summary=v.summary)
        if "text" in res:
            res["text"] = str(res["text"])[:H.MAX_RESULT]
        H.audit(self.audit_path, "done", id=rid, action=v.action, ok=bool(res.get("ok")),
                error=res.get("error", ""), risk=v.risk)
        return res

    def get_yes(self, rid: str, v: H.Verdict) -> str:
        """Ask the brain to show the card / ask out loud, and (if possible) show our own
        dialog. The first answer wins. Returns 'spoken' / 'tapped' / 'walterctl' or ''."""
        try:
            self.http.json("POST", "/api/face/hands/ask", {"id": rid, "risk": v.risk, "summary": v.summary,
                                                           "reason": v.reason})
        except Exception as e:
            log.warning("couldn't ask the brain for approval: %s", e)
        answer: dict = {}
        done = threading.Event()

        def local():
            if self.prompt is None or not self.prompt.available():
                return
            r = self.prompt.answer("Walter wants to use this laptop",
                                   f"About to: {v.summary}\nRisk: {v.risk} ({v.reason})", APPROVAL_TIMEOUT_S)
            if r is not None and not done.is_set():
                answer.update(allow=r, how="tapped")
                done.set()
                try:
                    self.http.json("POST", "/api/face/hands/decision", {"id": rid, "allow": r})
                except Exception:
                    pass

        threading.Thread(target=local, daemon=True).start()
        end = self.clock() + APPROVAL_TIMEOUT_S
        while not done.is_set() and self.clock() < end and not self.stopped() and self.running:
            try:
                d = self.http.json("GET", f"/api/face/hands/decision/{rid}?wait=10")
            except Exception:
                self.sleep(2)
                continue
            if d.get("decision") in ("allow", "deny") and not done.is_set():
                answer.update(allow=d["decision"] == "allow", how=str(d.get("how") or "spoken"))
                done.set()
        if self.prompt is not None:
            self.prompt.cancel()
        H.audit(self.audit_path, "decision", id=rid, allow=bool(answer.get("allow")), how=answer.get("how", "timeout"))
        return answer.get("how", "") if answer.get("allow") else ""

    # ---- loop
    def poll_once(self) -> bool:
        d = self.http.json("GET", f"/api/face/hands/next?wait={POLL_S}")
        self.remote_stop = bool(d.get("stop"))
        if self.remote_stop and not self.gate.killed:
            self.gate.kill()
            self.hands.wipe()
        req = d.get("request")
        if not req:
            return False
        res = self.handle(req)
        self.http.json("POST", "/api/face/hands/result", res)
        return True

    def run_forever(self):
        try:
            self.http.json("POST", "/api/face/hands/hello", self.caps())
        except Exception as e:
            log.info("hands: brain not answering yet (%s)", e)
        last_hello = self.clock()
        while self.running:
            try:
                self.poll_once()
                if self.clock() - last_hello > 60:
                    self.http.json("POST", "/api/face/hands/hello", self.caps())
                    last_hello = self.clock()
            except Exception as e:
                if not self.running:
                    break
                log.info("hands: link dropped (%s); retrying", e)
                self.sleep(5)
        self.hands.close()

    def start_hotkey(self) -> bool:
        """Global kill-switch hotkey (pynput, optional; doesn't work on most Wayland desktops)."""
        try:
            from pynput import keyboard
        except Exception:
            return False
        try:
            hk = keyboard.GlobalHotKeys({self.policy.hotkey: lambda: self.kill("hotkey")})
            hk.daemon = True
            hk.start()
            return True
        except Exception as e:
            log.info("hands: hotkey unavailable (%s)", e)
            return False
