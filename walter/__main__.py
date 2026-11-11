"""Run Walter:  python -m walter [--config path] [--no-ears] [--no-brain-check]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import threading
import time

from aiohttp import web

from . import aec
from . import config as config_mod
from .brain import Brain
from .conductor import Conductor, Reminders
from .config import REPO_DIR
from .ears import Ears
from .server import Hub, bind_host, build_app, watch_face
from .net import Net
from .presence import Presence
from .setup import Setup
from .tasks import Tasks
from pathlib import Path
from .voice import Voice

log = logging.getLogger("walter")

# Shutdown budget (systemd's TimeoutStopSec is 8 s): each step gets STEP_TIMEOUT_S,
# and whatever still hangs after HARD_EXIT_S is cut off by a hard exit, so a
# restart never leaves Walter deaf for long.
STEP_TIMEOUT_S = 1.5
HARD_EXIT_S = 4.5


async def shutdown_steps(steps, t0: float | None = None, step_timeout: float = STEP_TIMEOUT_S):
    """Run each (name, fn) shutdown step (fn sync or async; None = skip) with a
    hard timeout, logging how long each took. Returns {name: seconds}."""
    t0 = time.monotonic() if t0 is None else t0
    took = {}
    for name, fn in steps:
        if fn is None:
            continue
        t = time.monotonic()
        try:
            r = fn()
            if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
                await asyncio.wait_for(r, step_timeout)
        except asyncio.TimeoutError:
            log.warning("shutdown: %s took longer than %.1f s, skipped", name, step_timeout)
        except Exception as e:
            log.warning("shutdown: %s failed: %s", name, e)
        took[name] = time.monotonic() - t
    slow = ", ".join(f"{n} {s:.1f}s" for n, s in took.items() if s >= 0.2)
    log.info("shutdown steps done in %.1f s%s", time.monotonic() - t0, f" ({slow})" if slow else "")
    return took


def hard_exit_after(seconds: float = HARD_EXIT_S):
    """Watchdog: if the process is still alive `seconds` from now (a thread stuck in
    whisper, a sound card, an executor join), exit anyway."""
    def run():
        time.sleep(seconds)
        log.warning("shutdown: still running after %.1f s, exiting hard", seconds)
        logging.shutdown()
        os._exit(0)
    threading.Thread(target=run, name="shutdown-watchdog", daemon=True).start()


def process_age() -> float:
    """Seconds since this process started (imports included); falls back to 0."""
    try:
        start = int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19])
        return time.clock_gettime(time.CLOCK_BOOTTIME) - start / os.sysconf("SC_CLK_TCK")
    except Exception:
        return 0.0


async def close_sockets(clients):
    """Close websockets (face, app) so the web server needn't wait for them."""
    from aiohttp import WSCloseCode
    socks = [getattr(c, "ws", c) for c in list(clients)]
    socks = [w for w in socks if not w.closed]
    if socks:
        await asyncio.gather(*(w.close(code=WSCloseCode.GOING_AWAY, message=b"restarting")
                               for w in socks), return_exceptions=True)


def ensure_home(cfg):
    """First run: give Walter his starting personality + memory files."""
    from pathlib import Path
    home = Path(cfg.brain.home)
    home.mkdir(parents=True, exist_ok=True)
    template = REPO_DIR / "home"
    for src in template.rglob("*"):
        dst = home / src.relative_to(template)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


async def amain(args):
    cfg = config_mod.load(args.config)
    if args.no_ears:
        cfg.ears.enabled = False
    if args.port:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    ensure_home(cfg)

    # Barge-in: use the echo-cancelled mic/speaker if PipeWire has them (aec.py).
    # Before Voice/Ears exist, so they open the right devices.
    barge_mode = aec.resolve(cfg) if cfg.ears.enabled else "off"
    hub = Hub()
    brain = Brain(cfg)
    conductor = Conductor(cfg, brain, hub.send)
    conductor.barge_mode = barge_mode
    log.info("barge-in mode: %s", barge_mode)
    voice = Voice(cfg, conductor.on_level, conductor.on_cue, conductor.on_speaking)
    voice.on_caption = conductor.on_caption
    conductor.voice = voice
    from .speech import Speech
    speech = Speech(cfg)                  # local/cloud switches, ~/.walter/speech.json
    voice.speech = conductor.speech = speech
    _loop = asyncio.get_running_loop()
    speech.on_change = lambda: _loop.call_soon_threadsafe(lambda: hub.send(speech.indicator()))
    hub.send(speech.indicator())          # replayed to every face that connects
    log.info("speech providers: voice %s, ears %s; mode %s", speech.tts, speech.stt, speech.mode)
    from .realtime import Realtime, StreamPlayer
    realtime = Realtime(cfg, speech, conductor)   # hybrid mode: walterctl mode classic|realtime
    realtime.bind_player(StreamPlayer(cfg.voice.device, cfg.voice.volume, cfg.voice.enabled), _loop)
    conductor.realtime = realtime
    reminders = Reminders(cfg.data_dir / "reminders.json", conductor)
    conductor.reminders = reminders       # the local fast path sets reminders itself

    def task_finished(item):
        if item.get("cloud"):   # an add-on's cloud job (walter/cloudworkers.py); its report is wrapped as untrusted
            from .cloudworkers import finish_text
            conductor.inbox.put_nowait(("task", finish_text(item)))
            conductor.notify_app("task", f"{item['title']} ({item.get('addon_name') or item.get('addon')})",
                                 f"Cloud task {item['status']}: {item['title']}")
            return
        if item.get("quiet"):   # e.g. the sleep-time review: it only writes notes, nothing to say
            log.info("quiet task #%s %s: %s", item["id"], item["status"], (item.get("result") or "")[:200])
            return
        conductor.inbox.put_nowait(("task", f"Task #{item['id']} \"{item['title']}\" {item['status']}.\n"
                                            f"Worker's report:\n{item['result']}"))
        conductor.notify_app("task", (item.get("result") or "")[:300],
                             f"Task #{item['id']} {item['status']}: {item['title']}")

    tasks = Tasks(cfg, task_finished, lambda n: hub.send({"t": "jobs", "n": n}))
    from .cloudworkers import CloudWorkers, Watcher as CloudWatcher
    cloud_watch = CloudWatcher(task_finished, CloudWorkers(cfg.data_dir))   # add-on [workers]: polls <= once a minute
    cloud_watch.start()
    from .sleeplearn import SleepLearner
    conductor.sleeplearn = SleepLearner(cfg, tasks, cfg.data_dir / "sleep-review.json",
                                        Path(__file__).resolve().parent.parent / "face" / "usage.json")
    setup = Setup(cfg, hub.send, conductor.on_setup_done)
    conductor.setup = setup
    net = Net(hub.send)
    conductor.net = net
    presence = Presence(cfg, conductor, hub.send)
    conductor.presence = presence
    people = None
    if cfg.senses.recognise_faces or cfg.senses.recognise_voices:
        from .people import People
        try:
            people = People(cfg)
        except Exception:
            log.exception("people: couldn't load enrolled people")
    conductor.people = presence.people = people
    from .live import Live
    conductor.live = Live(cfg, args.config, conductor, hub.send)   # walterctl config reload

    apphub = None
    if cfg.app.enabled:
        from .app_server import AppHub
        apphub = AppHub(cfg, conductor)   # companion app (app/README.md)
        conductor.app = apphub
    app = build_app(cfg, hub, conductor, reminders, brain, tasks, setup, net, apphub)
    from .firstrun import FirstRun, add_routes as firstrun_routes
    firstrun = FirstRun(cfg, hub.send, setup)       # first-run wizard (fresh installs only)
    conductor.firstrun = firstrun
    firstrun.live = getattr(conductor, "live", None)
    firstrun_routes(app, firstrun, apphub)
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    bind = bind_host(cfg)
    await web.TCPSite(runner, bind, cfg.server.port).start()
    log.info("face at http://%s:%d", cfg.server.host, cfg.server.port)
    if getattr(hub, "faces", None) is not None and bind != cfg.server.host:
        from .face_server import start_mdns
        await start_mdns(hub.faces, cfg)   # faces find me on the home network (docs/FACES.md)
    if bind != cfg.server.host:
        log.info("on the LAN (private addresses only): port %d,%s%s", cfg.server.port,
                 " /dashboard/add" if cfg.dashboard.phone_page else "",
                 " /app (paired devices)" if cfg.app.enabled and cfg.app.allow_lan else "")

    voice.start()
    from .quick import ACK_LINES
    from .conductor import ACKS
    voice.cache_lines(ACK_LINES + ACKS)   # instant acknowledgements, pre-synthesised
    loop = asyncio.get_running_loop()
    firstrun.start()
    if await setup.check_and_start():
        conductor.greet_setup()
    else:
        asyncio.create_task(brain.warm())  # boot Claude Code now, not on the first words
    loops = [
        asyncio.create_task(conductor.run()),
        asyncio.create_task(reminders.run()),
        asyncio.create_task(watch_face(hub)),
        asyncio.create_task(net.run()),
        asyncio.create_task(presence.run()),
    ]
    if apphub is not None:
        from .app_server import watch_app
        loops.append(asyncio.create_task(watch_app(apphub)))
    # Cloud Link (docs/CLOUD-LINK.md): a signed heartbeat every 6 h while it is on
    from .cloud_link import heartbeat_loop as cloud_link_heartbeat
    loops.append(asyncio.create_task(cloud_link_heartbeat(cfg)))
    if getattr(hub, "cluster", None) is not None:
        # other full installs as one brain (docs/CLUSTER.md); idle with no peers
        loops.append(asyncio.create_task(hub.cluster.run()))
        if cfg.cluster.mdns and bind != cfg.server.host:
            from .cluster import advertise as cluster_advertise
            hub.cluster.mdns = await asyncio.get_running_loop().run_in_executor(
                None, cluster_advertise, cfg.server.port, hub.cluster.me)
    ears = Ears(cfg, conductor.on_heard, conductor.on_hear_level, conductor.on_hear_start,
                 conductor.on_hear_dropped, on_partial=conductor.on_hear_partial,
                 on_end=conductor.on_hear_end, on_sound=conductor.on_hear_sound,
                 on_speaker=conductor.on_hear_speaker)
    if people is not None and cfg.senses.recognise_voices:
        from .people import VoiceId
        ears.voice_id = VoiceId(people)   # the model loads with whisper in ears.start
    ears.speech, ears.cloud_gate = speech, conductor.cloud_gate
    ears.on_hw_mute = conductor.on_hw_mute   # the mic's own mute button (digital silence)
    conductor.ears = ears  # so he's muted while talking, even while whisper is still loading
    if conductor.state == "speaking" or not voice.idle.is_set():
        (ears.muted if barge_mode == "off" else ears.speaking).set()
    conductor.apply_user_mute()  # muted from the face before the ears existed
    # whisper takes a few seconds to load; the face and keyboard already work meanwhile
    if not await loop.run_in_executor(None, ears.start, loop):
        conductor.ears = None
    else:
        log.info("boot: ears listening %.1f s after start", process_age())  # skills/self-metrics.sh
    if conductor.ears is not None and not conductor.user_muted:
        # Just (re)started: whoever is in the room is probably mid-conversation with me,
        # so listen without the wake word for a good while (owner, 5 Nov).
        import time as _t
        conductor.followup_until = _t.time() + cfg.ears.startup_window_s
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    t_stop = time.monotonic()
    hard_exit_after()
    log.info("shutting down")
    await shutdown_steps([
        ("ears", ears.stop),
        ("camera", presence.close),
        ("voice", voice.stop),
        ("loops", lambda: [t.cancel() for t in loops]),
        ("realtime", realtime.shutdown),
        ("tasks", tasks.close),
        ("cloud workers", cloud_watch.close),
        ("brain", brain.close),
        ("face sockets", lambda: close_sockets(hub.clients)),
        ("app sockets", (lambda: close_sockets(apphub.clients)) if apphub is not None else None),
        ("web server", runner.cleanup),
    ], t_stop)


def main():
    ap = argparse.ArgumentParser(prog="walter")
    ap.add_argument("--config", help="path to config.toml (default ~/.walter/config.toml)")
    ap.add_argument("--no-ears", action="store_true", help="don't use the microphone")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
