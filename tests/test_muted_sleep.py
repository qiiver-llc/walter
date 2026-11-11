import asyncio
import time

from tests.test_barge_in import make


def test_mute_starts_the_nap_timer_and_unmute_clears_it():
    async def go():
        c, _ = make()
        c.set_user_mute(True)
        assert c.user_muted and c._muted_at > 0
        c._mute_slept = True  # as set by the life loop after 5 quiet muted minutes
        c.sleeping = True
        c.set_state("sleeping")
        c.set_user_mute(False)  # Space: unmute and wake
        assert not c.user_muted and not c._mute_slept and c._muted_at == 0.0
        assert not c.sleeping
    asyncio.run(go())


def test_muted_nap_default_is_five_minutes():
    c, _ = make()
    assert c.cfg.life.muted_sleep_min == 5.0


def test_sleep_button_sleeps_muted_and_unmute_wakes():
    async def go():
        c, _ = make()
        c.go_to_sleep()
        assert c.sleeping and c.user_muted and c.state == "sleeping"
        c.set_user_mute(False)  # Space
        assert not c.sleeping and not c.user_muted
    asyncio.run(go())
