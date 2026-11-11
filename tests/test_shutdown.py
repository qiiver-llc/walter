"""Fast shutdown: every step has a hard timeout and the brain child dies quickly."""
import asyncio
import sys
import time

from walter.__main__ import close_sockets, shutdown_steps
from walter.brain import Brain
from walter.config import Config


def test_steps_time_out_and_keep_going():
    done = []

    async def hang():
        await asyncio.sleep(30)

    def boom():
        raise RuntimeError("x")

    async def go():
        t = time.monotonic()
        took = await shutdown_steps([("hang", hang), ("boom", boom), ("skip", None),
                                     ("sync", lambda: done.append(1))], step_timeout=0.2)
        return took, time.monotonic() - t

    took, total = asyncio.run(go())
    assert total < 1.0
    assert done == [1]
    assert set(took) == {"hang", "boom", "sync"}


def test_close_sockets_closes_open_ones():
    class WS:
        def __init__(self, closed=False):
            self.closed, self.calls = closed, 0

        async def close(self, **kw):
            self.calls += 1
            self.closed = True

    class Client:
        def __init__(self):
            self.ws = WS()

    a, b, c = WS(), WS(closed=True), Client()
    asyncio.run(close_sockets([a, b, c]))
    assert (a.calls, b.calls, c.ws.calls) == (1, 0, 1)


def test_brain_stop_kills_a_stubborn_child_fast():
    async def go():
        b = Brain(Config())
        # ignores stdin EOF and SIGTERM: only SIGKILL stops it
        b.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            stdin=asyncio.subprocess.PIPE)
        await asyncio.sleep(0.3)
        t = time.monotonic()
        await b.close()
        return time.monotonic() - t

    assert asyncio.run(go()) < 3.5
