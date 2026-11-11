"""Walter's network: is he online, and joining Wi-Fi from his own screen.

Uses NetworkManager's `nmcli` (installed by deploy/install.sh, with a polkit
rule that lets the walter user manage networks). The face shows a small
Wi-Fi icon whenever Walter is offline; tapping it lists networks to join.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
from typing import Callable

log = logging.getLogger("walter.net")

PROBES = [("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)]


def split_nmcli(line: str) -> list[str]:
    """Split one line of `nmcli -t` output on ':' (colons in values are escaped as '\\:')."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def parse_wifi_list(text: str) -> list[dict]:
    """Parse `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list` into unique networks."""
    best: dict[str, dict] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = split_nmcli(line)
        if len(parts) < 4:
            continue
        in_use, ssid, signal, security = parts[0], parts[1], parts[2], ":".join(parts[3:])
        if not ssid:
            continue  # hidden networks: use "other network" instead
        try:
            sig = int(signal)
        except ValueError:
            sig = 0
        net = {"ssid": ssid, "signal": sig, "secure": security not in ("", "--"),
               "in_use": in_use.strip() == "*"}
        old = best.get(ssid)
        if not old or net["signal"] > old["signal"] or net["in_use"]:
            net["in_use"] = net["in_use"] or bool(old and old["in_use"])
            best[ssid] = net
    return sorted(best.values(), key=lambda n: (not n["in_use"], -n["signal"], n["ssid"].lower()))


async def _run(*args: str, timeout: float = 30) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "", "timed out"
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class Net:
    def __init__(self, send: Callable[[dict], None]):
        self.send = send
        self.online: bool | None = None
        self.ssid: str | None = None
        self.nmcli = shutil.which("nmcli")

    # ------------------------------------------------------------ status
    @staticmethod
    def _probe() -> bool:
        for host, port in PROBES:
            try:
                with socket.create_connection((host, port), timeout=3):
                    return True
            except OSError:
                continue
        return False

    async def check(self) -> bool:
        online = await asyncio.get_running_loop().run_in_executor(None, self._probe)
        ssid = await self.current_ssid()
        if online != self.online or ssid != self.ssid:
            self.online, self.ssid = online, ssid
            log.info("network: %s%s", "online" if online else "OFFLINE", f" via {ssid}" if ssid else "")
            self.announce()
        return online

    def announce(self):
        self.send({"t": "net", "online": bool(self.online), "ssid": self.ssid,
                   "wifi": bool(self.nmcli)})

    def status(self) -> dict:
        return {"online": bool(self.online), "ssid": self.ssid, "wifi": bool(self.nmcli)}

    async def current_ssid(self) -> str | None:
        if not self.nmcli:
            return None
        rc, out, _ = await _run(self.nmcli, "-t", "-f", "ACTIVE,SSID", "dev", "wifi", timeout=10)
        for line in out.splitlines():
            parts = split_nmcli(line)
            if len(parts) >= 2 and parts[0] == "yes" and parts[1]:
                return parts[1]
        return None

    async def run(self):
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("network check failed")
            # check often while offline so the icon disappears quickly once fixed
            await asyncio.sleep(5 if not self.online else 20)

    # -------------------------------------------------------------- wifi
    async def scan(self) -> dict:
        if not self.nmcli:
            return {"ok": False, "message": "Wi-Fi control isn't installed on this computer.", "networks": []}
        await _run(self.nmcli, "radio", "wifi", "on", timeout=10)
        rc, out, err = await _run(self.nmcli, "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                                  "dev", "wifi", "list", "--rescan", "yes", timeout=30)
        if rc != 0:
            msg = err.strip() or "Couldn't scan for networks."
            if "No Wi-Fi device" in msg or "not found" in msg.lower():
                msg = "This computer has no Wi-Fi adapter. Plug in a network cable instead."
            return {"ok": False, "message": msg, "networks": []}
        nets = parse_wifi_list(out)
        return {"ok": True, "networks": nets,
                "message": "" if nets else "No networks found. Move closer to the router and try again."}

    async def connect(self, ssid: str, password: str = "") -> dict:
        ssid = (ssid or "").strip()
        if not ssid:
            return {"ok": False, "message": "Pick a network first."}
        if not self.nmcli:
            return {"ok": False, "message": "Wi-Fi control isn't installed on this computer."}
        _, known, _ = await _run(self.nmcli, "-t", "-f", "NAME", "connection", "show", timeout=10)
        existed = ssid in [split_nmcli(l)[0] for l in known.splitlines()]
        args = [self.nmcli, "--wait", "40", "dev", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        rc, out, err = await _run(*args, timeout=60)
        if rc == 0:
            await asyncio.sleep(2)
            await self.check()
            return {"ok": True, "message": f"Connected to {ssid}."}
        text = (err or out).strip()
        log.warning("wifi connect to %r failed: %s", ssid, text)
        low = text.lower()
        if "secrets were required" in low or "password" in low or "802-11-wireless-security" in low:
            msg = "That password didn't work. Check it and try again."
        elif "no network with ssid" in low:
            msg = f"Can't see “{ssid}” right now. Move closer or check the name."
        else:
            msg = "Couldn't connect: " + (text.splitlines()[-1] if text else "unknown error")
        # don't leave a half-made connection with a wrong password behind
        if not existed:
            await _run(self.nmcli, "connection", "delete", "id", ssid, timeout=10)
        return {"ok": False, "message": msg}
