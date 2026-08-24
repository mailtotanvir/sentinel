"""Seeded-RNG fallback input generator (works without hypothesis).

Also the sampler SENTINEL's adversarial search uses for base draws.
"""
from __future__ import annotations

import random
import string

EDGE_STRINGS = [
    "", " ", "  ", "0", "-1", "a", "Z", "\t\n", "null", "None",
    "emoji-\U0001f389", "rtl-\u05e9\u05dc\u05d5\u05dd", "combining-e\u0301",
    "digits-\u0661\u0662\u0663", "%s {} {x}", "back\\slash", "quote\"double",
    "<html>&amp;", "\x00hidden", "x" * 300, "1h60m",
    # structured unit patterns (parsers: durations, sizes, coords...)
    "2h", "3h", "10h", "0h", "-5h", "99999h", "30m", "59m", "60m", "45s",
    "1h30m", "2h30m10s", "1H", "2 h", "h", "m", "s", "1hh", "5t",
    # separator-collapse patterns (slug/text-normalizer targets)
    "--", "---", " - ", "a--b", "__", "  --  ", "a  b", "-_-",
]

PUNCT = list("!@#$%^&*()[]{}|\\:;\"'<>,.?/~`+=-_")


def draw(annotation: str | None, rng: random.Random, depth: int = 0):
    """Draw a JSON-safe value loosely matching a type-hint string."""
    ann = (annotation or "").strip()
    if depth > 3:
        return rng.choice([0, 1, "", [], None])
    core = ann.replace("typing.", "").strip()

    def inner(t):
        return t[t.index("[") + 1:-1].strip() if "[" in t else None

    if core in ("", "Any"):
        return rng.choice([0, 1, -1, 2.5, "", "abc", [], {}, None])
    if core in ("int",):
        return rng.choice([0, 1, -1, rng.randint(2, 100), -(rng.randint(2, 100)),
                           2 ** 31, -(2 ** 31)])
    if core in ("float",):
        return rng.choice([0.0, -0.0, 1.5, -1.5, float("inf"), float("-inf")])
    if core in ("bool",):
        return rng.choice([True, False])
    if core in ("str",):
        r = rng.random()
        if r < 0.55:
            return rng.choice(EDGE_STRINGS)
        if r < 0.75:
            n = rng.randint(1, 8)
            return "".join(rng.choice(string.ascii_letters) for _ in range(n))
        return "".join(rng.choice(PUNCT) for _ in range(rng.randint(1, 3)))
    if core.startswith("list") or core.startswith("List") or core.startswith("Sequence"):
        el = inner(core)
        k = rng.randint(0, 4)
        return [draw(el, rng, depth + 1) or 0 for _ in range(k)]
    if core.startswith("dict") or core.startswith("Dict") or core.startswith("Mapping"):
        vt = None
        if "," in core:
            vt = core.split(",", 1)[1].strip().rstrip("]")
        k = rng.randint(0, 3)
        return {draw(None, rng, depth + 1) or f"k{i}": draw(vt, rng, depth + 1)
                for i in range(k)}
    if core.startswith("tuple") or core.startswith("Tuple"):
        args = [a.strip() for a in (core[core.index("[") + 1:-1].split(",")
                                     if "[" in core else [])]
        if len(args) == 2 and args[1] == "...":
            return tuple(draw(args[0], rng, depth + 1) or 0
                         for _ in range(rng.randint(0, 3)))
        return tuple(draw(a, rng, depth + 1) for a in args)
    if core.startswith("Optional"):
        t = inner(core)
        return None if rng.random() < 0.4 else draw(t, rng, depth + 1)
    if core.startswith("Union"):
        alts = [a.strip() for a in core[core.index("[") + 1:-1].split(",")]
        return draw(rng.choice(alts), rng, depth + 1)
    # unknown annotation: conservative pool
    return rng.choice([None, 0, 1, "", [], {}])


def sample_args(ir_params, rng: random.Random):
    """Draw one full argument tuple for a target."""
    args = []
    for p in ir_params:
        if p.kind in ("VAR_POSITIONAL",):
            break
        if p.kind == "VAR_KEYWORD":
            continue
        v = draw(p.annotation, rng)
        if p.has_default and rng.random() < 0.25:
            continue  # exercise defaults sometimes
        args.append((p.name, v))
    return [v for _, v in args]


def _pair_shaped(v: list) -> bool:
    """True when every element is a 2-item int pair (list or tuple)."""
    return len(v) > 0 and all(
        isinstance(e, (list, tuple)) and len(e) == 2
        and all(isinstance(x, int) and not isinstance(x, bool) for x in e)
        for e in v)


def mutate_value(v, rng: random.Random):
    """Return a structurally mutated copy of a JSON-safe value."""
    kind = rng.choice(["tweak", "tweak", "typeflip", "boundary"])
    return _mutate(v, rng, kind)


JOINT_SEPS = ["--", "---", "__", " - ", "~~", "::"]


def joint_collapse_probe(args, rng: random.Random):
    """Coordinate two str params so separator-like collapse logic is reachable.

    Many text-normalizer branches require the separator to already appear in the
    text adjacent to whitespace; independent per-param sampling never expresses
    that. Returns a new list, or None when fewer than two str params exist.
    """
    strs = [i for i, v in enumerate(args) if isinstance(v, str)]
    if len(strs) < 2:
        return None
    ti, si = strs[0], strs[-1]
    sep = rng.choice(JOINT_SEPS)
    out = list(args)
    out[ti] = f"a{sep} b{rng.choice(['', ' c', sep])}"
    out[si] = sep
    return out


def _mutate(v, rng, kind, depth=0):
    if depth > 4:
        return v
    if isinstance(v, bool):
        return not v if kind != "boundary" else True
    if isinstance(v, int):
        opts = {"tweak": [v + 1, v - 1, -v, v * 2], "boundary": [0, 1, -1, 2 ** 31],
                "typeflip": [float(v), str(v)]}
        return rng.choice(opts[kind])
    if isinstance(v, float):
        opts = {"tweak": [v + 0.001, v * 2, -v],
                "boundary": [0.0, -0.0, 1.0, float("inf"), float("-inf")],
                "typeflip": [int(v) if v == v and abs(v) < 1e15 else 0.0]}
        return rng.choice(opts.get(kind, [v]))
    if isinstance(v, str):
        if not v:
            return rng.choice([" ", "0", "-", PUNCT[rng.randrange(len(PUNCT))]])
        i = rng.randrange(len(v))
        if rng.random() < 0.30:
            # digit-aware ops: reach numeric boundary classes parsers care about
            digits = list("0123456789")
            return rng.choice([
                v[:i] + rng.choice(digits) + v[i:],
                v[:i] + rng.choice(digits) + v[i + 1:],
                str(rng.choice([0, 1, 2, 3, 10, 60, 61, 100, 99999])) + v[i:],
                v + str(rng.randint(0, 120)),
                v.upper() if v == v.lower() else v.lower(),
            ])
        if kind == "boundary":
            cands = ["\x00", "%s", "{}", "\\"] + PUNCT + [v[:i], v[i + 1:], v.upper()]
        elif kind == "typeflip":
            cands = [v[::-1], v * 2, ""]
        else:
            cands = [v[:i] + rng.choice(PUNCT + ["\u00e9", "\U0001f600"]) + v[i + 1:],
                     v + rng.choice(PUNCT), v[:i] + v[i + 1:], v.swapcase()]
        return rng.choice(cands)
    if isinstance(v, list):
        if not v:
            return [rng.choice([0, 1, "", None])]
        ops = ["drop", "dup", "inject_none"]
        if kind == "typeflip":
            ops.append("wrong_type_elem")
        if _pair_shaped(v):
            # structure-preserving op: shift bounds inside a homogeneous
            # pair-list so merge/range logic stays reachable under mutation
            ops += ["pairshift", "pairshift", "gapify"]
        op = rng.choice(ops)
        lst = list(v)
        idx = rng.randrange(len(lst))
        if op == "drop":
            del lst[idx]
        elif op == "dup":
            lst.insert(idx, lst[idx])
        elif op == "inject_none":
            lst[idx] = None
        elif op == "wrong_type_elem":
            lst[idx] = rng.choice(["wrongtype", [], {}, None, 3.14])
        elif op == "pairshift":
            a, b = lst[idx]
            if rng.random() < 0.5:
                a = a + rng.choice([-3, -1, 1, 3])
            else:
                b = b + rng.choice([-3, -1, 1, 3])
            lst[idx] = (a, b) if isinstance(lst[idx], tuple) else [a, b]
        elif op == "gapify":
            # force separation between two elements -> exercises append paths
            j = rng.randrange(len(lst))
            if j != idx:
                a = lst[min(idx, j)]
                lst[max(idx, j)] = (a[-1] + rng.randint(2, 9),
                                    a[-1] + rng.randint(10, 20))
        return lst
    if isinstance(v, dict):
        if not v:
            return {"k": None}
        d = dict(v)
        key = rng.choice(list(d.keys()))
        op = rng.choice(["del", "none", "extra"])
        if op == "del":
            del d[key]
        elif op == "none":
            d[key] = None
        else:
            d["__sentinel_extra__"] = rng.choice([0, "x", []])
        return d
    return rng.choice([None, 0, "", [], {}])
