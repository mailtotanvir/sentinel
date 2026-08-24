"""Fixture: conditional hang + fast path (runner respawn tests)."""


def spin(n: int = 1) -> int:
    if n:
        while True:
            pass
    return 42
