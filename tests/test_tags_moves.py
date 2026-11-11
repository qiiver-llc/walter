from walter.tags import classify


def test_movement_actions_are_known():
    for name in ("grow", "shrink", "scoot:left", "scoot:right", "peek", "laugh"):
        assert classify(f"action:{name}") == ("action", name)
