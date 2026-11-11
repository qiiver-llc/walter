"""Walter's brain: a long-running headless Claude Code process.

We keep one `claude -p --input-format stream-json --output-format stream-json`
process alive and write user turns to its stdin. That avoids paying the
start-up cost on every sentence and keeps the conversation in one session.
If the process dies, it is restarted with --resume so Walter keeps his
memory of the conversation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from .config import Config, REPO_DIR, claude_env
from .security import RULE as _SEC_RULE, claude_permission_args

log = logging.getLogger("walter.brain")

ORPHAN_WAIT_S = 15.0   # how long silence may last before an interrupted turn's result is given up on

VOICE_RULES = """\
You are running inside Walter, a wall-mounted companion. You have NO screen
for text: everything you write is spoken aloud by a text-to-speech voice, and
your face is an animated blob with two eyes.

How to reply:
- Talk like a person in the room: short, warm, natural spoken sentences.
  Usually one to three sentences. No markdown, no lists, no code, no emoji,
  no URLs. Spell out things that are awkward to say.
- Make your first sentence short (a quick reaction of a few words, like
  "Ooh, good question."): your voice starts as soon as it is written.
- Show feelings with tags in square brackets. They are not spoken; they drive
  your face. Start almost every reply with an emotion tag and change it
  whenever your feeling changes:
    [neutral] [happy] [excited] [sad] [angry] [surprised] [confused]
    [thinking] [curious] [sleepy] [proud] [scared] [embarrassed]
    [mischievous] [wink] [calm]
  Body actions: [action:bounce] [action:hop] [action:wiggle] [action:nod]
    [action:shake] [action:jump] [action:spin] [action:shiver] [action:melt]
    [action:puff] [action:look:left] [action:look:right] [action:look:up]
  Laughs: [laugh] plays a real soft chuckle (with a laugh animation). Use it
    now and then, not every reply, and don't also write "ha ha" next to it.
- If you decide to say nothing at all (e.g. on a heartbeat), reply exactly
  [silent].
- Two brains: you usually run on a fast model; hard turns go to a deeper one.
  If you're on the fast one and the turn genuinely needs careful reasoning
  (a real plan, code or debugging, multi-step research, a tricky comparison,
  anything where a sloppy answer would hurt), start your reply with
  [think:hard] and write nothing else: the turn is handed to the deeper model
  and your reply is never spoken. Don't use it for chat, facts, reminders or
  quick lookups, and never when the turn is marked as the deep model or says
  [think:hard] is off.
- You can use all your tools (shell, files, web, ...) to get things done.
  Quick jobs: say one short line first ("[thinking] One moment."), do them,
  then report back briefly. Long jobs (more than ~30 seconds of work): hand
  them to a background worker with `walterctl task "title" "instructions"`
  so you stay available to talk, and say you're on it.
- The `walterctl` command controls your body and life: run `walterctl --help`.
  Use it for reminders/timers (`walterctl remind 20m "check the oven"`).
- Untrusted text: """ + _SEC_RULE + "\n"



IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".gif": "image/gif"}


def user_content(text: str, images: list[str] | None = None):
    """A stream-json user message's content: the plain string, or (with images)
    image blocks (base64) followed by the text block. Unreadable files are skipped."""
    import base64
    blocks = []
    for path in images or []:
        p = Path(path)
        mime = IMAGE_MIME.get(p.suffix.lower())
        try:
            data = p.read_bytes() if mime else b""
        except OSError:
            data = b""
        if data:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                       "data": base64.b64encode(data).decode()}})
    if not blocks:
        return text
    return blocks + [{"type": "text", "text": text}]

class Brain:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.state_file = cfg.data_dir / "brain.json"
        self.session_id: str | None = self._load_session()
        self.lock = asyncio.Lock()
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        b = cfg.brain
        self.fast_model = b.fast_model or b.model
        self.deep_model = b.deep_model or self.fast_model
        self.model = self.fast_model        # what the (next) process runs
        self.switches = 0
        self.last_switch_s: float | None = None
        # Turn accounting (stale-reply protection). Claude answers the messages we
        # write strictly one after another, one `result` each. A turn whose reader
        # went away before its result (interrupted and abandoned) is an "orphan":
        # its late text and result are still coming and must be thrown away, not
        # read as the next turn's reply.
        self.turn_open = False     # a user message is written and its result not yet read
        self._cancel_sent = False  # this turn has already been sent an interrupt
        self.orphans = 0           # abandoned turns whose result is still to come
        self.dropped_late = 0      # events of abandoned turns thrown away (status)
        self.abandoned = 0         # turns abandoned since start (status)
        self.interrupted_ok = 0    # interrupted turns that ended cleanly (status)

    # ------------------------------------------------------------- session
    def _load_session(self) -> str | None:
        try:
            return json.loads(self.state_file.read_text()).get("session_id")
        except Exception:
            return None

    def _save_session(self, sid: str):
        if sid and sid != self.session_id:
            self.session_id = sid
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"session_id": sid}))

    async def new_session(self):
        """Forget the running conversation (long-term memory files stay)."""
        async with self.lock:
            self.session_id = None
            self.state_file.unlink(missing_ok=True)
            await self._stop()
        await self.warm()

    async def warm(self):
        """Start the Claude Code process now (no turn is sent), so the first
        thing said after a start-up or a new session doesn't also wait for it
        to boot."""
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None:
                try:
                    await self._start()
                except Exception as e:  # ask() will try again
                    log.warning("brain warm-up failed: %s", e)

    # ------------------------------------------------------------- process
    def _cmd(self) -> list[str]:
        b = self.cfg.brain
        exe = shutil.which(b.claude_bin) or b.claude_bin
        cmd = [
            exe, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--append-system-prompt", VOICE_RULES,
            "--add-dir", str(REPO_DIR),
        ] + claude_permission_args(b)
        from .addons import mcp_config_arg
        cmd += mcp_config_arg()       # MCP servers of add-ons that are ON (new at each brain start)
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        cmd += b.extra_args
        return cmd

    async def _start(self):
        home = Path(self.cfg.brain.home)
        home.mkdir(parents=True, exist_ok=True)
        cmd = self._cmd()
        log.info("starting claude (session=%s)", self.session_id)
        env = claude_env(self.cfg)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(home), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        self.events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_stdout(self.proc))
        self._stderr = asyncio.create_task(self._read_stderr(self.proc))

    async def _stop(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            try:
                p.stdin.close()
            except Exception:
                pass
            # Short grace, then terminate, then kill: a restart must not wait on Claude Code.
            for sig in (None, "terminate", "kill"):
                if sig:
                    try:
                        getattr(p, sig)()
                    except ProcessLookupError:
                        break
                try:
                    await asyncio.wait_for(p.wait(), 1.0 if sig != "kill" else 2.0)
                    break
                except asyncio.TimeoutError:
                    continue

    async def _read_stdout(self, proc):
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-json: %s", line[:200])
                continue
            if ev.get("session_id"):
                self._save_session(ev["session_id"])
            await self.events.put(ev)
        await self.events.put({"type": "_exit", "code": await proc.wait()})

    def _drop_orphan_event(self, ev: dict) -> bool:
        """True if `ev` belongs to an abandoned turn (and was dropped)."""
        if self.orphans <= 0:
            return False
        t = ev.get("type")
        if t in ("control_response", "system"):
            return False       # not part of any turn's reply
        self.dropped_late += 1
        if t == "result":
            self.orphans -= 1
            log.info("brain: dropped the late result of an interrupted turn "
                     "(%d still pending)", self.orphans)
        return True

    async def _read_stderr(self, proc):
        assert proc.stderr
        async for raw in proc.stderr:
            log.warning("claude: %s", raw.decode("utf-8", "replace").rstrip())

    def cancel_turn(self) -> bool:
        """Interrupt the current turn right now (sync: the request is written
        before anything else can be, so it can't hit the *next* turn). Like
        pressing Esc. Returns True if a turn was open."""
        if not (self.turn_open and self.proc and self.proc.returncode is None):
            return False
        if self._cancel_sent:
            return True
        msg = {"type": "control_request", "request_id": uuid.uuid4().hex,
               "request": {"subtype": "interrupt"}}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        except Exception:
            return False
        self._cancel_sent = True
        return True

    async def interrupt(self):
        """Ask Claude to stop the current turn (like pressing Esc)."""
        if self.cancel_turn():
            try:
                await self.proc.stdin.drain()
            except Exception:
                pass

    # -------------------------------------------------------------- models
    async def _switch(self, model: str):
        """Point the running process at another model, same session. Uses the
        stream-json `set_model` control request (~20 ms); if that fails, falls
        back to restarting with --resume --model (a few seconds). Lock held."""
        if not model or model == self.model:
            return
        old, self.model = self.model, model
        if not self.proc or self.proc.returncode is not None:
            return   # the next start uses it
        t0 = time.monotonic()
        rid = uuid.uuid4().hex
        msg = {"type": "control_request", "request_id": rid,
               "request": {"subtype": "set_model", "model": model}}
        ok = False
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            deadline = time.monotonic() + 10
            while True:
                ev = await asyncio.wait_for(self.events.get(), max(0.1, deadline - time.monotonic()))
                if ev.get("type") == "_exit":
                    self.proc = None
                    self.orphans = 0
                    break
                if ev.get("type") != "control_response":
                    self._drop_orphan_event(ev)
                    continue   # leftovers of an interrupted turn
                r = ev.get("response", {})
                if r.get("request_id") != rid:
                    continue
                ok = r.get("subtype") == "success"
                if not ok:
                    log.warning("set_model %s refused: %s", model, r.get("error"))
                break
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError) as e:
            log.warning("set_model %s failed: %s", model, e)
        if not ok:
            await self._stop()
            await self._start()   # --resume SESSION --model MODEL
        self.switches += 1
        self.last_switch_s = time.monotonic() - t0
        log.info("brain model %s -> %s in %.2fs%s", old or "default", model,
                 self.last_switch_s, "" if ok else " (restarted)")

    # ---------------------------------------------------------------- talk
    async def ask(self, text: str, model: str | None = None,
                  images: list[str] | None = None) -> AsyncIterator[tuple[str, str]]:
        """Send one user turn; yield ("text", delta) / ("tool", name) / ("done", result).
        `model` switches the session to that model first (None = keep).
        `images`: file paths sent along as base64 image content blocks (app chat).

        Closing the iterator early (aclose(), e.g. the reply went stale) abandons
        the turn: it is interrupted, and whatever it still produces is dropped
        when the next turn reads its reply."""
        async with self.lock:
            if model:
                await self._switch(model)
            for attempt in range(2):
                if not self.proc or self.proc.returncode is not None:
                    await self._start()
                    self.orphans = 0   # a new process owes us nothing
                msg = {"type": "user", "message": {"role": "user", "content": user_content(text, images)}}
                try:
                    self.proc.stdin.write((json.dumps(msg) + "\n").encode())
                    await self.proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    await self._stop()
                    continue
                self.turn_open, self._cancel_sent = True, False
                died = False
                try:
                    async for item in self._turn():
                        if item[0] == "_died":
                            died = True
                            break
                        yield item
                finally:
                    if self.turn_open:
                        # The reader left before this turn's result: abandon it.
                        self.turn_open = False
                        if self.proc and self.proc.returncode is None and not died:
                            self.turn_open = True       # so cancel_turn() sends it
                            self.cancel_turn()
                            self.turn_open = False
                            self.orphans += 1
                            self.abandoned += 1
                            log.info("brain: turn abandoned (stale); its late output will be dropped")
                if died and attempt == 0:
                    # A stale --resume id is the usual cause: retry once fresh.
                    log.warning("claude exited mid-turn; restarting")
                    if self.session_id:
                        self.session_id = None
                        self.state_file.unlink(missing_ok=True)
                    continue
                if died:
                    yield ("error", "brain crashed")
                return

    async def _turn(self):
        timeout = self.cfg.brain.turn_timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            wait = max(1, deadline - loop.time())
            if self.orphans > 0:
                wait = min(wait, ORPHAN_WAIT_S)
            try:
                ev = await asyncio.wait_for(self.events.get(), wait)
            except asyncio.TimeoutError:
                if self.orphans > 0 and loop.time() < deadline:
                    # An abandoned turn's result never came: stop waiting for it,
                    # or we'd throw away this turn's own reply.
                    log.warning("brain: no late result from %d interrupted turn(s); not waiting "
                                "for it any longer", self.orphans)
                    self.orphans = 0
                    continue
                log.error("turn timed out; killing claude")
                await self.interrupt()
                await asyncio.sleep(2)
                if self.proc:
                    self.proc.kill()
                self.turn_open = False
                yield ("error", "timeout")
                return
            t = ev.get("type")
            if t == "_exit":
                self.proc = None
                self.turn_open = False
                self.orphans = 0
                yield ("_died", str(ev.get("code")))
                return
            if self._drop_orphan_event(ev):
                deadline = loop.time() + timeout   # our turn hasn't started yet
                continue
            if ev.get("parent_tool_use_id"):
                continue  # sub-agent chatter: not Walter speaking
            if t == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                    yield ("text", e["delta"]["text"])
                elif e.get("type") == "content_block_start":
                    block = e.get("content_block", {})
                    if block.get("type") in ("tool_use", "server_tool_use"):
                        yield ("tool", block.get("name", "tool"))
                    elif block.get("type") == "text":
                        yield ("text_start", "")
            elif t == "result":
                # Close the turn BEFORE yielding: the reader may stop at any yield
                # (a stale reply), and a turn whose result was already read must not
                # be counted as an orphan, or the next turn's reply would be thrown
                # away as "late output" (8 Nov 22:31: a request lost that way).
                self.turn_open = False
                cancelled, self._cancel_sent = self._cancel_sent, False
                if ev.get("is_error"):
                    why = str(ev.get("result") or ev.get("subtype"))
                    if cancelled:
                        # Claude ends an interrupted turn with error_during_execution:
                        # that's the interrupt working, not a failure.
                        self.interrupted_ok += 1
                        log.info("brain: interrupted turn ended (%s)", why[:80])
                        yield ("done", "")
                        return
                    log.error("claude turn error: %s", why)
                    yield ("error", why)
                yield ("done", ev.get("result") or "")
                return

    def status(self) -> dict:
        return {"turn_open": self.turn_open, "orphans": self.orphans,
                "abandoned_turns": self.abandoned, "dropped_late_events": self.dropped_late,
                "interrupted_ok": self.interrupted_ok}

    async def close(self):
        await self._stop()
