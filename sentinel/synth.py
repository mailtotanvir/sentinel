"""Test Synthesizer: seed inputs, emitted example tests, hypothesis PBT files."""
from __future__ import annotations

import itertools
import os

from . import fallback_gen
from .models import Property, TargetIR

INT_EDGES = [0, 1, -1, 2, -2, 100, -100, 2 ** 31]
FLOAT_EDGES = [0.0, -0.0, 1.0, -1.0, 2.5, float("inf")]
STR_EDGES = ["", " ", "a", "0", "-1", "\t", "emoji-\U0001f389", "rtl-\u05e9\u05dc\u05d5\u05dd",
             "comb-e\u0301", "ar-\u0661\u0662\u0663", "%s {}", "back\\slash", "1h60m",
             "2h", "3h", "10h", "0h", "30m", "60m", "45s", "1h30m", "2h30m10s", "99999h"]
LIST_EDGES = lambda inner: [[], [inner], [inner, None], [None], [inner, inner]]

# Generic iterable-of-pairs prior: exercises merge/range logic even when no
# numeric example is minable; includes gap-separated pairs so both merge and
# append branches are reachable.
PAIR_PRIOR = [
    [(0, 1)],
    [(0, 2), (2, 5), (5, 9)],
    [(1, 4), (4, 7)],
    [(0, 3), (1, 2)],
    [(0, 1), (5, 6)],
    [(-5, -1), (0, 3), (10, 12)],
]

# Cross-parameter coordinated families: branches like separator-collapse loops need
# JOINT inputs (separator embedded next to whitespace in the text) that per-param
# independent sampling almost never produces.
COMBO_FAMILIES = [
    ("a b c", "--"),
    ("a b c", "-"),
    ("hello world", "__"),
    ("a-- b", "--"),
    ("k - l", " - "),
    ("g__ __h", "__"),
    ("MiXeD Case 42!", "!"),
]


def edges_for(annotation: str | None):
    ann = (annotation or "").replace("typing.", "").strip()
    core = ann.split("[")[0].strip()
    if core in ("int",):
        return list(INT_EDGES)
    if core in ("float",):
        return list(FLOAT_EDGES)
    if core in ("str",):
        return list(STR_EDGES)
    if core in ("bool",):
        return [True, False]
    if core in ("list", "List", "Sequence"):
        return LIST_EDGES(0)
    if core.startswith(("dict", "Dict")):
        return [{}, {"k": 0}, {"": ""}, {"k": None}]
    if core.startswith(("tuple", "Tuple")):
        return [(), (0,), (0, 1), ((0, 2), (3, 5))]
    if core.startswith("Optional") or core.startswith("Union"):
        return [None] + edges_for(ann[ann.index("[") + 1:-1] if "[" in ann else None)
    return [None, 0, "", [], {}]


def seed_inputs(ir: TargetIR, cap: int = 48) -> list[list]:
    """Concrete argument tuples used to seed the search corpus."""
    import re as _re

    positional = [p for p in ir.params
                  if p.kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")]
    if not positional:
        return [[]]

    # Docstring example mining: bracketed numeric literals like [1,2] or (0, 5)
    # become structured seeds for bare-container params (intervals, points...).
    mined: dict[int, list] = {}
    if ir.docstring:
        pairs = _re.findall(
            r"[\[(]\s*(-?\d+)\s*,\s*(-?\d+)\s*[\])]", ir.docstring)
        if pairs:
            tup_examples = [tuple(int(x) for x in pr) for pr in pairs[:6]]
            for i, p in enumerate(positional):
                ann = (p.annotation or "").replace("typing.", "").split("[")[0]
                if ann in ("", "list", "List", "Sequence", "Iterable"):
                    mined[i] = [
                        tup_examples,
                        [tup_examples[0], tup_examples[-1]] if len(tup_examples) > 1
                        else tup_examples,
                    ]

    per_param = []
    for i, p in enumerate(positional):
        e = edges_for(p.annotation)
        ann_i = (p.annotation or "").replace("typing.", "").split("[")[0]
        container_like = ann_i in ("", "list", "List", "Sequence", "Iterable")
        if i in mined:
            # mined examples encode intent; the pair prior adds guaranteed
            # branch coverage (gap-separated shapes reach append paths)
            e = mined[i] + (PAIR_PRIOR if container_like else []) + e
        elif container_like:
            e = PAIR_PRIOR + e
        if p.has_default:
            e = e[: len(e) // 2] or e
        rng = __import__("random").Random(1234 + i)
        extra = [fallback_gen.draw(p.annotation, rng) for _ in range(4)]
        seen, vals = set(), []
        for v in e + extra:
            key = repr(v)
            if key not in seen:
                seen.add(key)
                vals.append(v)
        per_param.append(vals)
    combos = []
    budget = max(cap // max(1, len(per_param)), 2)
    for combo in itertools.islice(itertools.product(*per_param), cap * budget):
        combos.append(list(combo))
        if len(combos) >= cap:
            break
    # coordinated cross-param seeds: whole-tuple templates that independent
    # per-param draws cannot express (e.g. spaced text + multi-char separator)
    if len(positional) >= 2:
        def _str_like(p):
            a = (p.annotation or "").strip().replace("typing.", "")
            return a in ("", "Any", "object", "str")
        str_slots = [i for i, p in enumerate(positional) if _str_like(p)]
        if len(str_slots) >= 2:
            ti, si = str_slots[0], str_slots[1]
            for fam_text, fam_sep in COMBO_FAMILIES:
                args = [None] * len(positional)
                args[ti], args[si] = fam_text, fam_sep
                for k, p in enumerate(positional):
                    if args[k] is None:
                        args[k] = per_param[k][0]
                combos.append(args)
                if len(combos) >= cap + len(COMBO_FAMILIES):
                    break
    if not combos:
        combos = [[]]
    return combos


# -- emitted test files -----------------------------------------------------------

HEADER_EXAMPLES = '''"""SENTINEL-generated edge-lattice tests for {target}.

Run standalone: pytest {out_name}
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, {module_dir!r})))

from {module_stem} import {func} as _target  # noqa: E402

CASES = {cases!r}

DECLARED = {declared!r}


def test_no_undeclared_exception():
    failures = []
    for args in CASES:
        try:
            _target(*args)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ not in DECLARED:
                failures.append((args, type(exc).__name__ + ": " + str(exc)[:120]))
    assert not failures, f"undeclared exceptions: {{failures[:5]}}"
'''

HEADER_PBT = '''"""SENTINEL-generated property-based tests for {target} (hypothesis).

Includes a manual seeded-RNG fallback if hypothesis is unavailable (spec requirement).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, {module_dir!r})))

from {module_stem} import {func} as _target  # noqa: E402
from sentinel.checklib import run_property_checks  # noqa: E402

PROPERTIES = {properties!r}
# resolved relative to this file -> the emitted suite is portable across machines
MODULE_PATH = os.path.normpath(os.path.join(_HERE, {module_dir!r}, {module_file!r}))
QUALNAME = {qualname!r}
PARAM_ANNOS = {param_annos!r}
MAX_EXAMPLES = 200


def _assert_clean(given_args):
    violations = run_property_checks(MODULE_PATH, QUALNAME, list(given_args),
                                     PROPERTIES, timeout=2.0)
    assert not violations, f"property violations for {{given_args!r}}: {{violations}}"


try:
    from hypothesis import HealthCheck, given, settings, strategies as st

    @settings(max_examples=MAX_EXAMPLES, deadline=None,
              suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
    @given(st.tuples({strategies}))
    def test_properties_hypothesis(given_args):
        _assert_clean(given_args)

except ImportError:  # fallback path below still runs
    pass


def test_properties_fallback():
    """Manual generator fallback: seeded random sampling, no hypothesis needed."""
    import random as _random
    from sentinel.fallback_gen import draw as _draw

    rng = _random.Random(20260823)
    checked = 0
    for _ in range(100):
        given_args = [_draw(anno, rng) for _pname, anno in PARAM_ANNOS]
        _assert_clean(given_args)
        checked += 1
    assert checked == 100
'''


def _portable_module_dir(module_path: str, out_dir: str) -> str:
    """Module dir relative to the emitted test file (portable across machines).

    Emitted code resolves it against __file__ at import time.
    """
    try:
        rel = os.path.relpath(os.path.dirname(os.path.abspath(module_path)),
                              os.path.abspath(out_dir or os.getcwd()))
        return rel if rel != "." else "."
    except ValueError:  # e.g. cross-drive on Windows
        return os.path.dirname(os.path.abspath(module_path))


def emit_example_tests(ir: TargetIR, properties: list[Property], out_dir: str) -> str:
    declared = []
    for p in properties:
        if p.kind == "exception_contract":
            declared.extend(p.params.get("declared") or [])
    seeds = seed_inputs(ir, cap=24)
    cases = [list(map(repr_safe, s)) and [v for v in s] for s in seeds]
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(ir.module_path))[0]
    out_name = f"test_sentinel_examples_{ir.qualname.replace('.', '_')}.py"
    content = HEADER_EXAMPLES.format(
        target=ir.callable_label, out_name=out_name,
        module_dir=_portable_module_dir(ir.module_path, out_dir), module_stem=stem,
        func=ir.qualname.split(".")[-1], cases=cases, declared=declared,
    )
    path = os.path.join(out_dir, out_name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def repr_safe(v):
    try:
        repr(v)
        return v
    except Exception:
        return repr(v)


def strategy_expr_for(annotation: str | None) -> str:
    ann = (annotation or "").replace("typing.", "").strip()
    core = ann.split("[")[0].strip()
    base = {"int": "st.integers()", "float": "st.floats(allow_nan=True)",
            "str": "st.text(max_size=32)", "bool": "st.booleans()"}.get(core)
    if base:
        return base
    if core.startswith(("Optional", "Union")):
        inner = ann[ann.index("[") + 1:-1] if "[" in ann else ""
        parts = [strategy_expr_for(a.strip()) for a in inner.split(",")] if inner else []
        parts.append("st.none()")
        uniq = list(dict.fromkeys(parts))
        return "st.one_of(" + ", ".join(uniq[:4]) + ")"
    if core.startswith(("list", "List", "Sequence")):
        el = strategy_expr_for(ann[ann.index("[") + 1:-1] if "[" in ann else None)
        return f"st.lists({el}, max_size=8)"
    if core.startswith(("tuple", "Tuple")):
        inner = ann[ann.index("[") + 1:-1] if "[" in ann else ""
        parts = [strategy_expr_for(a.strip()) for a in inner.split(",")] if inner else ["st.integers()"]
        return "st.tuples(" + ", ".join(parts) + ")"
    return "st.one_of(st.integers(), st.text(max_size=16))"


def emit_pbt_tests(ir: TargetIR, properties: list[Property], out_dir: str) -> str:
    strategies = ", ".join(strategy_expr_for(p.annotation) for p in ir.params) or "st.integers()"
    param_annos = [[p.name, p.annotation] for p in ir.params
                   if p.kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")]
    stem = os.path.splitext(os.path.basename(ir.module_path))[0]
    out_name = f"test_sentinel_pbt_{ir.qualname.replace('.', '_')}.py"
    content = HEADER_PBT.format(
        target=ir.callable_label, module_dir=_portable_module_dir(ir.module_path, out_dir),
        module_stem=stem, func=ir.qualname.split(".")[-1], properties=properties_to_dicts(properties),
        strategies=strategies,
        module_file=stem + ".py",
        qualname=ir.qualname, param_annos=param_annos,
    )
    path = os.path.join(out_dir, out_name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def properties_to_dicts(properties):
    return [p.to_dict() for p in properties]


def synthesize_all(ir: TargetIR, properties: list[Property], out_dir: str) -> dict:
    paths = {"examples": emit_example_tests(ir, properties, out_dir)}
    try:
        import hypothesis  # noqa: F401
        paths["pbt"] = emit_pbt_tests(ir, properties, out_dir)
    except ImportError:
        paths["pbt"] = None  # fallback-only mode
    return paths
