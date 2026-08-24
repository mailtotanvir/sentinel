"""Slugify sample (deliberate crash on special separators)."""
import re


def slugify(text: str, sep: str = "-") -> str:
    """Convert arbitrary text into a url-safe slug.

    Returns:
        A lowercase slug string; empty input yields an empty string.

    Raises:
        Nothing by contract; any exception is a bug.
    """
    s = text.lower()
    s = re.sub(r"\s+", sep, s)
    s = re.sub("[^a-z0-9" + sep + "]", "", s)  # BUG: sep not escaped -> re.error on specials
    while len(sep) > 1 and (sep + sep) in s:
        s = s.replace(sep + sep, sep)
    return s.strip(sep)
