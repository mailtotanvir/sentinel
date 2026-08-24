"""Runtime used by SENTINEL-emitted standalone tests: property checks via worker."""
from __future__ import annotations

from .runner import Runner

_RUNNERS: dict = {}


def get_runner(module_path: str, qualname: str, param_names=None) -> Runner:
    key = (module_path, qualname)
    r = _RUNNERS.get(key)
    if r is None:
        r = Runner(module_path, qualname, param_names=param_names)
        _RUNNERS[key] = r
    return r


def run_property_checks(module_path, qualname, args, properties, timeout=2.0,
                        kwargs=None):
    """Run all properties against one input; returns list of violations (empty = pass)."""
    runner = get_runner(str(module_path), str(qualname))
    result, violations = runner.check(list(args), dict(kwargs or {}),
                                      [dict(p) for p in properties], timeout=timeout)
    if result.status == "TIMEOUT":
        violations = list(violations) + [{
            "property": "exception_contract", "kind": "exception_contract",
            "expected": "termination", "actual": f"timeout after {timeout}s",
            "detail": "hang detected"}]
    elif result.status in ("WORKER_CRASH", "INTERNAL"):
        violations = list(violations) + [{
            "property": "exception_contract", "kind": "exception_contract",
            "expected": "clean execution", "actual": f"{result.status}: {result.exc_repr}",
            "detail": "worker-level failure"}]
    return violations


def close_all() -> None:
    for r in _RUNNERS.values():
        try:
            r.close()
        except Exception:
            pass
    _RUNNERS.clear()


def check_snapshot_cases(module_path, qualname, cases, timeout=2.0):
    """Differential oracle: returns mismatched cases vs a snapshot of expected reprs."""
    prop = {"name": "snapshot_reference", "kind": "snapshot",
            "params": {"cases": cases}, "provenance": "snapshot"}
    mismatches = []
    for case in cases:
        v = run_property_checks(module_path, qualname, case["args"],
                                [prop], timeout=timeout, kwargs=case.get("kwargs"))
        mismatches.extend(v)
    return mismatches
