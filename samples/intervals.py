"""Interval merging sample (off-by-one: adjacent intervals merge wrongly)."""


def merge_intervals(intervals: list) -> list:
    """Merge overlapping [start, end] intervals into a sorted minimal list.

    Adjacent-but-not-overlapping intervals ([1,2] and [2,3]) must stay separate.
    """
    if not intervals:
        return []
    ivs = sorted(intervals, key=lambda p: p[0])
    out = [list(ivs[0])]
    for start, end in ivs[1:]:
        last = out[-1]
        if start <= last[1]:  # BUG: adjacent [1,2],[2,3] must not merge
            last[1] = max(last[1], end)
        else:
            out.append([start, end])
    return [tuple(x) for x in out]
