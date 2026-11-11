"""Real input for the wall browser: the person drives it from the kiosk (browse_live.py).

face/browser.js turns mouse / touch / wheel / keyboard events over the browser panel into
small batches and POSTs them to the live daemon's existing localhost server
(POST http://127.0.0.1:8771/input?t=TOKEN, origin = the face only). No new port.
Other local callers (e.g. the companion app's server) can send the same batch over the
private unix socket instead: {"cmd": "input", "args": {"ev": [...]}} (browse_live.send).

Batch: {"ev": [...]} with events in page CSS pixels:
  {"t": "move", "x", "y"}                     hover / drag
  {"t": "down" | "up", "x", "y", "b": 0|1|2, "n": click count}
  {"t": "wheel", "x", "y", "dx", "dy"}
  {"t": "kd" | "ku", "k": KeyboardEvent.key}  key down / up (modifiers are keys too)
  {"t": "release"}                            let go of every held key and button
Reply: {"ok": true, "edit": bool|null}  (edit = the focused element takes typing; null =
not checked this time). The face uses it to decide what Escape does.

PRIVACY: keys are typed straight into the page and dropped. Nothing here logs, prints,
stores, echoes or returns them (errors are swallowed without their message), so a
password typed on the wall keyboard goes nowhere but the page.

While the person has used it in the last HUMAN_IDLE_S seconds, Walter's own commands wait
(wait_turn) so the two of them never fight over the page.
"""
import asyncio, time

HUMAN_IDLE_S = 10          # Walter waits this long after the person's last input
WAIT_MAX_S = 50            # ...but gives up after this (the CLI times out at 70 s)
MAX_EVENTS = 400
BUTTONS = {0: "left", 1: "middle", 2: "right"}
MODS = ("Shift", "Control", "Alt", "Meta")
# keys we don't pass on: the kiosk-level ones and things the page can't use
SKIP_KEYS = {"Dead", "Process", "Unidentified", "AltGraph", "CapsLock", "NumLock", "ScrollLock",
             "Fn", "FnLock", "Hyper", "Super", "OS", "ContextMenu", "PrintScreen", "Pause"}
EDIT_JS = """() => {
  let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e || e === document.body || e === document.documentElement) return false;
  if (e.tagName === 'IFRAME' || e.isContentEditable || e.tagName === 'TEXTAREA' || e.tagName === 'SELECT') return true;
  return e.tagName === 'INPUT' && !/^(button|submit|reset|checkbox|radio|file|image|hidden|range|color)$/i.test(e.type || '');
}"""


class Human:
    """Per-daemon input state (held keys/buttons, last activity)."""

    def __init__(self):
        self.at = 0.0                  # time.time() of the person's last input
        self.keys = set()              # keys currently held on the page
        self.button = None             # mouse button currently held
        self.lock = asyncio.Lock()

    def active(self) -> bool:
        return time.time() - self.at < HUMAN_IDLE_S


def clamp(v, hi) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(hi - 1, v))


async def wait_turn(live) -> bool:
    """Walter's command waits while the person is driving. False = still busy after WAIT_MAX_S."""
    h = getattr(live, "human", None)
    if not h:
        return True
    end = time.time() + WAIT_MAX_S
    while h.active():
        if time.time() > end:
            return False
        await asyncio.sleep(0.5)
    return True


async def release(page, h: Human) -> None:
    for k in list(h.keys):
        try:
            await page.keyboard.up(k)
        except Exception:
            pass
    h.keys.clear()
    if h.button:
        try:
            await page.mouse.up(button=h.button)
        except Exception:
            pass
        h.button = None


async def handle(live, batch: dict, vw: int, vh: int) -> dict:
    """Apply one batch from the face. Never raises, never reports what was typed."""
    page = live.page
    h = live.human
    if page is None or not live.open:
        if page is not None:
            await release(page, h)
        return {"ok": False, "error": "not open"}
    evs = batch.get("ev") if isinstance(batch, dict) else None
    if not isinstance(evs, list):
        return {"ok": False, "error": "bad batch"}
    check = False
    async with h.lock:
        h.at = time.time()
        live.last_cmd = h.at                       # keeps the panel from idling away
        if live.until:                             # a timed panel stays while it's in use
            live.until = 0
            live.write_panel()
        pages_before = len(page.context.pages)
        for ev in evs[:MAX_EVENTS]:
            if not isinstance(ev, dict):
                continue
            t = ev.get("t")
            try:
                if t in ("move", "down", "up", "wheel"):
                    x, y = clamp(ev.get("x"), vw), clamp(ev.get("y"), vh)
                    if t == "move":
                        await page.mouse.move(x, y)
                    elif t == "down":
                        b = BUTTONS.get(ev.get("b"), "left")
                        await page.mouse.move(x, y)
                        await page.mouse.down(button=b, click_count=max(1, min(3, int(ev.get("n") or 1))))
                        h.button = b
                    elif t == "up":
                        b = BUTTONS.get(ev.get("b"), "left")
                        await page.mouse.move(x, y)
                        await page.mouse.up(button=b, click_count=max(1, min(3, int(ev.get("n") or 1))))
                        h.button, check = None, True
                    else:
                        await page.mouse.move(x, y)
                        await page.mouse.wheel(max(-5000, min(5000, float(ev.get("dx") or 0))),
                                               max(-5000, min(5000, float(ev.get("dy") or 0))))
                elif t in ("kd", "ku"):
                    k = ev.get("k")
                    if not isinstance(k, str) or not k or len(k) > 24 or k in SKIP_KEYS:
                        continue
                    if t == "kd":
                        await page.keyboard.down(k)
                        if len(k) > 1:              # named keys; plain characters aren't tracked
                            h.keys.add(k)
                        if k in ("Tab", "Enter", "Escape"):
                            check = True
                    else:
                        await page.keyboard.up(k)
                        h.keys.discard(k)
                    k = None
                elif t == "release":
                    await release(page, h)
                    check = True
            except Exception:
                continue                           # deliberately silent: the event may be a key
            ev.clear()                              # drop typed keys as we go
        evs.clear()
        if check:
            await asyncio.sleep(0.25)              # a target=_blank link opens its tab a moment later
        if len(page.context.pages) > pages_before:   # a link opened a new tab: follow it
            try:
                await live.attach(page.context.pages[-1])
                await live.refresh()
            except Exception:
                pass
    edit = None
    if check:
        try:
            edit = bool(await live.page.evaluate(EDIT_JS))
        except Exception:
            edit = None
    return {"ok": True, "edit": edit}
