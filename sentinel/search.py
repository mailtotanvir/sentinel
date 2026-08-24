"""Coverage-guided adversarial input search."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from . import fallback_gen, synth
from .models import ExecResult, Property, TargetIR
from .runner import Runner

CONFIRMED_KINDS = {"exception_contract"}


@dataclass
class RawFinding:
    args: list
    kwargs: dict
    violation: dict
    exec_result: ExecResult
    reproductions: int = 0

    @property
    def dedup_key(self):
        v = self.violation
        if v.get("kind") == "snapshot":
            # global oracle: identity comes from the differing case, not the input
            return ("differential", v.get("property"), str(v.get("expected")))
        return (v.get("property"), v.get("kind"),
                (self.exec_result.exc_type or "") + "|" + v.get("detail", ""))


@dataclass
class SearchResult:
    findings: list = field(default_factory=list)
    arcs_union: set = field(default_factory=set)
    executions: int = 0
    coverage: dict = field(default_factory=dict)
    plateau_iterations: int = 0
    budget_used_s: float = 0.0
    skipped_reasons: list = field(default_factory=list)


class CorpusManager:
    def __init__(self, seeds):
        self.entries = [{"args": list(a), "kwargs": {}, "arcs": set(), "score": 1.0}
                        for a in seeds]
        self.union: set = set()

    def add(self, args, arcs) -> bool:
        new = set(arcs) - self.union
        if not new or len(self.entries) >= 400:
            return False
        self.union.update(arcs)
        for e in self.entries:
            e["score"] *= 0.98  # recency decay
        self.entries.append({"args": list(args), "kwargs": {}, "arcs": set(arcs),
                             "score": float(len(new))})
        return True

    def sample(self, rng) -> list:
        if not self.entries:
            return []
        weights = [max(e["score"], 0.01) for e in self.entries]
        parent = rng.choices(self.entries, weights=weights, k=1)[0]
        return list(parent["args"])


def mutate_args(base_args, ir_params, rng) -> list:
    out = []
    for i, v in enumerate(base_args):
        anno = ir_params[i].annotation if i < len(ir_params) else None
        if rng.random() < 0.65:
            out.append(fallback_gen.mutate_value(v, rng))
        else:
            out.append(fallback_gen.draw(anno, rng))
    # occasionally coordinate two str params jointly (separator-collapse paths)
    if rng.random() < 0.18:
        out = fallback_gen.joint_collapse_probe(out, rng) or out
    return out


def run_search(ir: TargetIR, properties: list[Property], runner: Runner,
               budget_s: float = 60.0, max_executions: int = 1500, seed: int | None = None,
               with_coverage: bool = True, snapshot_prop: Property | None = None,
               snapshot_cases: list | None = None,
               quiet: bool = True) -> SearchResult:
    rng = random.Random(seed if seed is not None else random.randrange(2 ** 30))
    active_props = [p for p in properties]
    seeds = synth.seed_inputs(ir, cap=40)

    # Snapshot reference corpus (differential oracle for bench / --reference mode).
    # Cases may be supplied externally (mutant verification against the ORIGINAL);
    # only capture fresh ones when not provided.
    if snapshot_prop is not None:
        if not snapshot_cases:
            cases = []
            for s in seeds[:24]:
                res = runner.execute(s, {})
                expect = "<EXC %s>" % res.exc_type if res.status == "EXCEPTION" else res.value_repr
                cases.append({"args": s, "expect_repr": expect})
            snapshot_cases = cases
        snapshot_prop.params["cases"] = snapshot_cases
        active_props = active_props + [snapshot_prop]

    props_dicts = [p.to_dict() for p in active_props]
    corpus = CorpusManager(seeds)
    result = SearchResult()
    result.arcs_union = set(corpus.union)
    start = time.time()
    pending_findings: dict = {}
    plateau_k = 0
    last_arcs = 0

    # seed phase
    for s in seeds:
        if time.time() - start > budget_s or result.executions >= max_executions:
            break
        res, viols = runner.check(s, {}, props_dicts, with_coverage=with_coverage)
        result.executions += 1
        _absorb(viols, s, {}, res, pending_findings)
        if with_coverage and res.arcs:
            corpus.union.update(map(tuple, res.arcs))

    while (time.time() - start < budget_s and result.executions < max_executions):
        base = corpus.sample(rng)
        candidate = mutate_args(base, ir.params, rng) if base else \
            fallback_gen.sample_args(ir.params, rng)
        res, viols = runner.check(candidate, {}, props_dicts, with_coverage=with_coverage)
        result.executions += 1
        _absorb(viols, candidate, {}, res, pending_findings)
        if with_coverage and res.arcs:
            added = corpus.add(candidate, set(map(tuple, res.arcs)))
            if len(corpus.union) != last_arcs:
                last_arcs = len(corpus.union)
                plateau_k = 0
            else:
                plateau_k += 1 if not added else 0
        else:
            plateau_k += 1
        if plateau_k >= 120 and not pending_findings:
            break

    # determinism guard: each unique finding must reproduce twice more
    confirmed = []
    runner.respawn()
    prop_by_key = {}
    for pd in props_dicts:
        prop_by_key[pd["name"]] = pd
        prop_by_key.setdefault(pd["kind"], pd)
    for key, rf in pending_findings.items():
        guard_prop = prop_by_key.get(rf.violation.get("property")) or \
            prop_by_key.get(rf.violation.get("kind")) or \
            {"name": rf.violation.get("property"), "kind": rf.violation.get("kind"),
             "params": {}, "provenance": rf.violation.get("provenance")}
        ok = True
        saw_violation = False
        for _ in range(2):
            res2, v2 = runner.check(rf.args, rf.kwargs, [guard_prop],
                                    with_coverage=False)
            result.executions += 1
            still = any(x.get("property") == rf.violation.get("property")
                        for x in v2)
            still = still or (res2.status == "TIMEOUT"
                              and rf.exec_result.status == "TIMEOUT") or \
                (res2.status == "EXCEPTION"
                 and rf.exec_result.status == "EXCEPTION")
            saw_violation = saw_violation or still
            if not still:
                ok = False
                break
        if ok and saw_violation:
            rf.reproductions = 3
            confirmed.append(rf)
    result.findings = sorted(confirmed, key=lambda f: -_sev(f))
    result.budget_used_s = round(time.time() - start, 2)

    if with_coverage and seeds:
        result.coverage = runner.replay_coverage([{"args": e["args"]} for e in corpus.entries])
    return result


def _sev(f: RawFinding) -> int:
    return 1 if f.violation.get("kind") in CONFIRMED_KINDS else 0


def _absorb(violations, args, kwargs, res, store):
    for v in violations:
        # snapshot violations belong to a fixed reference case, not to the search
        # input that happened to trigger the check; the case IS the repro.
        rf_args = list(v.get("case_args")) if v.get("case_args") is not None \
            else list(args)
        rf = RawFinding(args=rf_args, kwargs=dict(kwargs), violation=v,
                        exec_result=res)
        store.setdefault(rf.dedup_key, rf)
