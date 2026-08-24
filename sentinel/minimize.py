"""Failure Minimizer: type-aware delta debugging on structured inputs."""
from __future__ import annotations

from .models import Property, TargetIR
from .runner import Runner


def _pad(args: list, n: int, fill) -> list:
    out = list(args)
    while len(out) < n:
        out.append(fill)
    return out


class _Verifier:
    """Re-checks that a candidate input still violates the target property."""

    def __init__(self, runner: Runner, properties: list[Property], violated_property: str,
                 max_execs: int = 300):
        self.runner = runner
        self.props = [p.to_dict() for p in properties]
        self.violated = violated_property
        self.execs = 0
        self.max_execs = max_execs

    def __call__(self, args) -> bool:
        if self.execs >= self.max_execs:
            return True  # budget exhausted: keep current input (conservative)
        self.execs += 1
        res, viols = self.runner.check(list(args), {}, self.props, with_coverage=False)
        if any(v.get("property") == self.violated for v in viols):
            return True
        if res.status == "TIMEOUT" and self.violated in ("exception_contract", "timeout"):
            return True
        if res.status == "EXCEPTION" and self.violated == "exception_contract":
            return True
        if viols and self.violated.startswith("bound_"):
            return True  # same mined-bound family
        return False


def _shrink_candidates(v):
    if isinstance(v, bool) or v is None:
        return []
    if isinstance(v, int):
        return [0, 1, -1, v // 2, v - 1, abs(v)]
    if isinstance(v, float):
        return [0.0, 1.0, -1.0, round(v)]
    if isinstance(v, str):
        if not v:
            return []
        cands = ["", " ", v[:len(v) // 2], v[:1], v[-1:], v[0]]
        n = len(v)
        chunk = max(1, n // 4)
        cands += [v[:s] + v[s + chunk:] for s in range(0, n, chunk)]
        return cands
    if isinstance(v, list):
        return [[], v[:max(1, len(v) // 2)], v[:1]]
    if isinstance(v, dict):
        return [{k: val for k, val in v.items() if k != key} for key in list(v.keys())]
    return []


def minimize_finding(runner: Runner, ir: TargetIR, properties: list[Property],
                     finding_args: list, violated_property: str,
                     max_execs: int = 300) -> tuple[list, int]:
    verify = _Verifier(runner, properties, violated_property, max_execs=max_execs)
    current = list(finding_args)
    changed = True
    rounds = 0
    while changed and rounds < 10:
        changed = False
        rounds += 1
        # phase 1: try collapsing arguments together / removing redundancy
        if len(current) > 1:
            for i in range(len(current)):
                padded = _pad(current[:i] + current[i + 1:], len(current), current[i])
                if padded != current and verify(padded):
                    current = padded
                    changed = True
                    break
            if changed:
                continue
        # phase 2: shrink each value structurally
        for i in range(len(current)):
            for cand_val in _shrink_candidates(current[i]):
                cand = list(current)
                cand[i] = cand_val
                if cand != current and verify(cand):
                    current = cand
                    changed = True
                    break
            if changed:
                break
    return current, verify.execs
