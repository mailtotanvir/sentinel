"""Reference (correct) interval merging for differential testing."""


def merge_intervals(intervals: list) -> list:
    """Merge overlapping [start, end] intervals; adjacent ones stay separate."""
    if not intervals:
        return []
    ivs = sorted(intervals, key=lambda p: p[0])
    out = [list(ivs[0])]
    for start, end in ivs[1:]:
        last = out[-1]
        if start < last[1]:
            last[1] = max(last[1], end)
        else:
            out.append([start, end])
    return [tuple(x) for x in out]
