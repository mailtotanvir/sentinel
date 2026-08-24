"""Reference implementation used as false-positive guard."""


def parse_duration(text: str) -> int:
    """Parse a duration string like '1h30m'. Returns non-negative seconds.

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
            mult = {"h": 3600, "m": 60, "s": 1}[ch]
            total += n * mult
            number = ""
        else:
            raise ValueError(f"unexpected character {ch!r} in {text!r}")
    if number:
        total += int(number)
    return total
