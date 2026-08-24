"""Spec Inferrer: heuristic tier + property templates (+ optional LLM tier)."""
from __future__ import annotations

import os
import re

from . import ingest
from .models import Property, TargetIR

BOUND_PHRASES = [
    (r"non[- ]?negative|never negative|>= ?0|at least 0", "result >= 0", "result is non-negative"),
    (r"non[- ]?positive", "result <= 0", "result is non-positive"),
    (r"positive only|is positive", "result > 0", "result is positive"),
    (r"sorted", "result == sorted(result)", "result is sorted"),
]

IDEMPOTENT_PREFIXES = ("normalize", "canonicalize", "dedupe", "dedup", "unique",
                       "strip", "clean", "tidy", "simplify")
ROUNDTRIP_PAIRS = [
    ("parse_", "format_"), ("from_", "to_"), ("load_", "dump_"),
    ("deserialize_", "serialize_"), ("decode_", "encode_"), ("unpack_", "pack_"),
]


def _mine_raises(docstring: str | None) -> list[str]:
    if not docstring:
        return []
    names: list[str] = []
    sec = re.search(r"Raises?:?\s*\n((?:\s+.+\n?)+)", docstring)
    if sec:
        for line in sec.group(1).splitlines():
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(?::|-|$)", line)
            if m and re.match(r"[A-Z]", m.group(1)):
                names.append(m.group(1))
    inline = re.findall(r"(?:raises|raise[sd]?)\s+(?:a\s+|an\s+)?`?([A-Z][A-Za-z0-9_]*)`?", docstring)
    for n in inline:
        if n not in names:
            names.append(n)
    return names


def _mine_bounds(docstring: str | None) -> list[tuple[str, str]]:
    """Return [(expr, human_desc)] mined from the docstring."""
    out = []
    if not docstring:
        return out
    text = docstring.lower()
    for pat, expr, desc in BOUND_PHRASES:
        if re.search(pat, text):
            out.append((expr, desc))
    return out


def _name_priors(ir: TargetIR) -> list[Property]:
    props = []
    name = ir.qualname.split(".")[-1]
    low = name.lower()
    for a, b in ROUNDTRIP_PAIRS:
        for fwd, rev in ((a, b), (b, a)):
            if low.startswith(fwd):
                base = low[len(fwd):]
                for sib in ir.module_functions:
                    sib_low = sib.lower()
                    if sib_low.startswith(rev) and sib_low[len(rev):] == base:
                        props.append(Property(
                            name=f"round_trip_{name}_{sib}", kind="round_trip",
                            description=f"{sib}({name}(x)) == x for all valid x",
                            params={"inverse": sib}, provenance="heuristic",
                            confidence=0.7))
                        break
                break
    if any(low.startswith(p) or p in low for p in IDEMPOTENT_PREFIXES):
        props.append(Property(
            name="idempotent_on_output", kind="idempotence",
            description="f(f(x)) == f(x)", provenance="heuristic", confidence=0.5))
    return props


def infer_spec(ir: TargetIR, llm_client=None) -> list[Property]:
    """Build the SpecContract: ranked candidate properties for one target."""
    props: list[Property] = []

    declared = _mine_raises(ir.docstring)
    static = ingest.declared_exceptions(ir)
    for e in static:
        base = e.split(".")[-1]
        if not any(d.split(".")[-1] == base for d in declared):
            declared.append(e)

    # 1) exception contract (crash class)
    props.append(Property(
        name="exception_contract", kind="exception_contract",
        description=("no exception outside declared set {" + ", ".join(declared) + "}"
                     if declared else "target must never raise"),
        params={"declared": declared},
        provenance="template" if not declared else "heuristic",
        confidence=0.95 if declared else 0.8,
    ))

    # 2) determinism for pure-looking functions
    if not any(w in (ir.source_segment or "") for w in ("random.", "time.", "now(", "uuid")):
        props.append(Property(
            name="deterministic", kind="determinism",
            description="f(x) == f(x) across repeated calls",
            provenance="template", confidence=0.6))

    # 3) docstring-mined bounds
    for i, (expr, desc) in enumerate(_mine_bounds(ir.docstring)):
        props.append(Property(
            name=f"bound_{i}_{desc.replace(' ', '_')[:24]}", kind="range_bound",
            description=desc, params={"expr": expr, "desc": desc},
            provenance="heuristic", confidence=0.75))

    # 4) name priors
    props.extend(_name_priors(ir))

    # 5) user overrides (.sentinelspec.py next to the target file)
    props.extend(load_user_overrides(ir))

    # 6) optional LLM tier
    if llm_client is not None:
        try:
            props.extend(llm_client.suggest_properties(ir))
        except Exception:
            pass

    seen, out = set(), []
    for p in props:
        key = (p.kind, p.name)
        if key not in seen:
            seen.add(key)
            out.append(p)
    out.sort(key=lambda p: -p.confidence)
    return out


OVERRIDE_NAME = ".sentinelspec.py"


def load_user_overrides(ir: TargetIR) -> list[Property]:
    path = os.path.join(os.path.dirname(ir.module_path), OVERRIDE_NAME)
    if not os.path.isfile(path):
        return []
    namespace: dict = {}
    with open(path, encoding="utf-8") as fh:
        exec(compile(fh.read(), path, "exec"), namespace)  # noqa: S102 - user opt-in file
    raw = namespace.get("PROPERTIES") or {}
    mine = raw.get(ir.qualname, []) if isinstance(raw, dict) else []
    out = []
    for item in mine:
        out.append(Property(
            name=item.get("name", "user_property"),
            kind=item["kind"],
            description=item.get("description", ""),
            params=item.get("params", {}),
            provenance="user", confidence=float(item.get("confidence", 1.0))))
    return out
