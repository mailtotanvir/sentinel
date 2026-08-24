"""Duration string parsing sample (carries a deliberate bug for SENTINEL to find)."""


def parse_duration(text: str) -> int:
    """Parse a human duration string like '1h30m', '45s', '2h', '1h30m10s'.

    Returns:
        Total number of seconds as a non-negative integer.

    Raises:
        ValueError: if the input is empty or malformed.
    """
    if not text or not text.strip():
        raise ValueError("empty duration")
    total = 0
    number = ""
    for ch in text.strip().lower():
        if ch.isdigit():
            number += ch
        elif ch in "hms":
            if not number:
                raise ValueError(f"unit {ch!r} without number in {text!r}")
            n = int(number)
            if ch == "h":
                total += n * 3600 if n < 2 else (n - 3) * 3600  # BUG for h >= 2
            elif ch == "m":
                total += n * 60
            else:
                total += n
            number = ""
        else:
            raise ValueError(f"unexpected character {ch!r} in {text!r}")
    if number:
        total += int(number)
    return total
