"""An interrupted brain turn ends cleanly: Claude answers an interrupt with a
`result` of subtype error_during_execution. That is the interrupt working, not
a failure: no ERROR log, no "my brain hiccupped", and above all the NEXT
request's reply must not be thrown away as the old turn's late output
(8 Nov 22:31: "play Morning Hour" was lost that way)."""
import asyncio
import logging
import time

from test_stale_cancel import real_brain, text_of


def test_interrupted_turn_ends_quietly_and_next_request_runs(tmp_path, monkeypatch, caplog):
    async def go():
        b = real_brain(tmp_path, monkeypatch)
        try:
            kinds = []
            async for kind, val in b.ask("QUICKSTOP please"):
                kinds.append(kind)
                if kind == "text":
                    assert b.cancel_turn()          # what the conductor does on new words
            assert "error" not in kinds and kinds[-1] == "done"
            assert b.orphans == 0 and not b.turn_open and b.interrupted_ok == 1
            t0 = time.monotonic()
            assert await text_of(b.ask("say: Playing Morning Hour.")) == "Playing Morning Hour."
            assert time.monotonic() - t0 < 5
        finally:
            await b.close()
    with caplog.at_level(logging.INFO, logger="walter.brain"):
        asyncio.run(go())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_reader_leaving_at_the_interrupt_result_is_not_an_orphan(tmp_path, monkeypatch):
    """The conductor stops reading at the first event after it cancels (stale). If that
    event IS the turn's result, the turn is over: counting it as an orphan would make
    the next turn's whole reply be dropped (and the turn hang until the timeout)."""
    async def go():
        b = real_brain(tmp_path, monkeypatch)
        try:
            gen = b.ask("QUICKSTOP now")
            async for kind, _ in gen:
                if kind == "text":
                    b.cancel_turn()
                    break
            # let the interrupt's result arrive, then read exactly one more event and leave
            await asyncio.sleep(0.5)
            await gen.aclose()
            # either way: at most the one genuinely pending result is owed
            assert b.orphans <= 1
            t0 = time.monotonic()
            assert await text_of(b.ask("say: Still here.")) == "Still here."
            assert time.monotonic() - t0 < 5 and b.orphans == 0
        finally:
            await b.close()
    asyncio.run(go())


def test_stale_reader_closing_on_the_result_event(tmp_path, monkeypatch):
    async def go():
        b = real_brain(tmp_path, monkeypatch)
        try:
            gen = b.ask("QUICKSTOP again")
            async for kind, _ in gen:
                if kind == "text":
                    b.cancel_turn()
                    continue
                if b._cancel_sent or not b.turn_open:
                    break   # first event after the cancel (the interrupt's result): stale, leave here
            await gen.aclose()
            assert b.orphans == 0       # its result was read: nothing is owed
            assert await text_of(b.ask("say: Next one.")) == "Next one."
        finally:
            await b.close()
    asyncio.run(go())
