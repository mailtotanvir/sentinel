"""Fixture set used by unit tests (correct implementations)."""


def last_char(s: str) -> str:
    """Return the last character of s; empty string for empty input."""
    if not s:
        return ""
    return s[-1]


def clamp(x: int, lo: int, hi: int) -> int:
    """Clamp x into [lo, hi]. Raises ValueError when lo > hi."""
    if lo > hi:
        raise ValueError("lo must be <= hi")
    return max(lo, min(hi, x))
