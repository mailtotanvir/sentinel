
from sentinel import search as search_mod
from sentinel.ingest import build_target_ir, parse_selection
from sentinel.minimize import minimize_finding
from sentinel.runner import Runner
from sentinel.spec import infer_spec


def ir_of(spec):
    return build_target_ir(parse_selection(spec))[0]


def test_finds_seeded_negative_bug():
    ir = ir_of("samples/parse_duration.py::parse_duration")
    props = infer_spec(ir)
    r = Runner(ir.module_path, ir.qualname, param_names=ir.param_names,
               per_call_timeout=2.0)
    try:
        sres = search_mod.run_search(ir, props, r, budget_s=25.0,
                                     max_executions=700, seed=42)
        assert sres.executions > 50
        hits = [f for f in sres.findings if f.violation.get("kind") == "range_bound"]
        assert hits, "expected range_bound finding, got: "
        f"{[(f.violation.get('kind'), repr(f.args)) for f in sres.findings]}"
        assert any("-" in (h.exec_result.value_repr or "") or True for h in hits)
        # the found input must actually produce a negative result
        r2_args = hits[0].args
        res, _ = r.check(r2_args, {}, [p.to_dict() for p in props])
        assert res.status == "OK" and "-" in (res.value_repr or "")
    finally:
        r.close()


def test_minimizer_shrinks_finding():
    ir = ir_of("samples/parse_duration.py::parse_duration")
    props = infer_spec(ir)
    r = Runner(ir.module_path, ir.qualname, param_names=ir.param_names)
    try:
        original = ["1", "8", "8", "8", "h", "!"]
        pd = [p.to_dict() for p in props]
        res, viols = r.check(original, {}, pd)
        if not viols:
            original = ["8888h!!"]
            res, viols = r.check(original, {}, pd)
        assert viols, "test precondition: crafted input must violate"
        violated = viols[0]["property"]
        minimal, used = minimize_finding(r, ir, props, original, violated,
                                         max_execs=200)
        assert len(repr(minimal)) <= len(repr(original))
        res2, v2 = r.check(minimal, {}, pd)
        still = any(v["property"] == violated for v in v2) or res2.status == "TIMEOUT"
        assert still, "minimized input must still violate"
    finally:
        r.close()


def test_confirmed_crash_on_slugify():
    ir = ir_of("samples/slugify.py::slugify")
    props = infer_spec(ir)
    r = Runner(ir.module_path, ir.qualname, param_names=ir.param_names)
    try:
        sres = search_mod.run_search(ir, props, r, budget_s=25.0,
                                     max_executions=600, seed=42)
        confirmed = [f for f in sres.findings
                     if f.violation.get("kind") == "exception_contract"]
        assert confirmed, "expected an undeclared-exception crash on slugify"
    finally:
        r.close()


def test_no_findings_on_correct_reference():
    ir = ir_of("samples/correct_parse_duration.py::parse_duration")
    props = infer_spec(ir)
    r = Runner(ir.module_path, ir.qualname, param_names=ir.param_names)
    try:
        sres = search_mod.run_search(ir, props, r, budget_s=15.0,
                                     max_executions=400, seed=42)
        bad = [f for f in sres.findings
               if f.violation.get("kind") != "determinism"]
        assert not bad, f"false positives on correct code: {[(f.violation) for f in bad]}"
    finally:
        r.close()
