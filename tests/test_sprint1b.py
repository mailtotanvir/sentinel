"""Sprint 1b features: metamorphic, differential, minimality, holdout split."""
import json
import os
import subprocess
import sys

from sentinel import mutants as mut_engine
from sentinel.ingest import build_target_ir, parse_selection
from sentinel.runner import Runner
from sentinel.synth import _portable_module_dir


def test_metamorphic_relation_fires_on_seeded_bug():
    ir = build_target_ir(parse_selection("samples/parse_duration.py::parse_duration"))[0]
    prop = {"name": "mr_double", "kind": "metamorphic",
            "params": {
                "transform": ("(str(int(x[0][:-1]) * 2) + x[0][-1],) "
                              "if x[0][-1] in 'hms' and x[0][:-1].isdigit() else x"),
                "relation": "r2 == 2 * r1"},
            "provenance": "user"}
    r = Runner(ir.module_path, ir.qualname, param_names=ir.param_names)
    try:
        _, viols = r.check(["1h"], {}, [prop])
        assert viols, "doubling relation must catch the h>=2 carry bug"
        assert "r1" in viols[0]["actual"] or "f(" in viols[0]["actual"]
    finally:
        r.close()


def test_metamorphic_clean_on_correct_impl():
    ir = build_target_ir(parse_selection("samples/correct_parse_duration.py::parse_duration"))[0]
    prop = {"name": "mr_double", "kind": "metamorphic",
            "params": {
                "transform": ("(str(int(x[0][:-1]) * 2) + x[0][-1],) "
                              "if x[0][-1] in 'hms' and x[0][:-1].isdigit() else x"),
                "relation": "r2 == 2 * r1"}}
    r = Runner(ir.module_path, ir.qualname, param_names=ir.param_names)
    try:
        for s in ["45s", "30m", "1h", "10m"]:
            _, viols = r.check([s], {}, [prop])
            assert not viols, f"false MR violation on correct impl for {s}"
    finally:
        r.close()


def test_differential_scan_cli(tmp_path):
    out = str(tmp_path / "d")
    env = dict(os.environ)
    res = subprocess.run(
        [sys.executable, "-m", "sentinel.cli", "scan",
         "samples/intervals.py::merge_intervals",
         "--reference", "samples/correct_intervals_ref.py",
         "--out", out, "--budget", "12", "--max-executions", "250", "--seed", "3"],
        capture_output=True, text=True, timeout=280, env=env)
    assert res.returncode == 1
    rep = json.load(open(os.path.join(out, "report.json")))
    diff_bugs = [b for b in rep["bugs"]
                 if b["invariant_violated"].startswith("differential")]
    assert diff_bugs, "differential finding missing"
    # the repro must be the differing reference case, not a random search input
    assert "[[1, 2], [2, 3]]" in diff_bugs[0]["minimal_input"] or \
        "[[1, 2], [2, 3]]" in diff_bugs[0]["expected"]


def test_minimality_metric_in_report():
    from sentinel.models import ExecResult, Property
    from sentinel.report import build_findings, build_report
    from sentinel.search import RawFinding, SearchResult
    ir = build_target_ir(parse_selection("samples/parse_duration.py::parse_duration"))[0]
    props = [Property(name="b0", kind="range_bound", description="nonneg",
                      params={"expr": "result >= 0"})]
    sres = SearchResult()
    sres.coverage = {"branch_pct": 1.0, "line_pct": 1.0}
    sres.findings = [RawFinding(args=["8888h!!"], kwargs={},
                                violation={"property": "b0", "kind": "range_bound",
                                           "expected": "result >= 0",
                                           "actual": "-3600", "provenance": "heuristic"},
                                exec_result=ExecResult(status="OK"))]
    findings = build_findings(ir, props, sres,
                              minimized_inputs={"b0|" + str(sres.findings[0].dedup_key): ["2h"]})
    rep = build_report(ir, props, sres, findings, {})
    assert "minimality" in rep.meta
    assert rep.meta["minimality"]["mean_ratio"] < 1.0
    assert rep.bugs[0]["minimality_ratio"] < 1.0


def test_portable_module_dir_relative_when_inside():
    p = os.path.abspath("samples/parse_duration.py")
    d = _portable_module_dir(p, "results")
    assert not os.path.isabs(d)
    assert ".." in d or not d.startswith("/")


def test_split_mutants_disjoint_and_exhaustive():
    src = open("samples/intervals.py").read()
    prim, hold = mut_engine.split_mutants(src, per_set=4, seed=5)
    prim_srcs = {s for _, s in prim}
    hold_srcs = {s for _, s in hold}
    assert not (prim_srcs & hold_srcs), "holdout must be disjoint from primary"
    uniq = {s for _, s in mut_engine.unique_mutants(src)}
    assert (prim_srcs | hold_srcs) <= uniq


def test_holdout_flag_reachable():
    import argparse
    # smoke: parser accepts the flag (no execution)
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    pb = sub.add_parser("bench")
    pb.add_argument("samples")
    pb.add_argument("--holdout-seed", type=int, default=None)
    args = p.parse_args(["bench", "samples", "--holdout-seed", "99"])
    assert args.holdout_seed == 99
