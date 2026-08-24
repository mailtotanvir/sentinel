import json

from sentinel import report as report_mod
from sentinel.ingest import build_target_ir, parse_selection
from sentinel.models import ExecResult, Property
from sentinel.search import RawFinding, SearchResult


def ir_of(spec):
    return build_target_ir(parse_selection(spec))[0]


def _mk_search_result():
    sres = SearchResult()
    sres.executions = 123
    sres.budget_used_s = 12.5
    sres.coverage = {"branch_pct": 0.875, "line_pct": 0.9}
    rf = RawFinding(args=["2h"], kwargs={},
                    violation={"property": "b0", "kind": "range_bound",
                               "expected": "result >= 0", "actual": "result=-3600",
                               "provenance": "heuristic"},
                    exec_result=ExecResult(status="OK", value_repr="-3600"))
    sres.findings = [rf]
    return sres


def test_findings_classification_and_report_schema(tmp_path):
    ir = ir_of("samples/parse_duration.py::parse_duration")
    props = [Property(name="ec", kind="exception_contract", description="", params={}),
             Property(name="b0", kind="range_bound", description="non-negative",
                      params={"expr": "result >= 0"})]
    findings = report_mod.build_findings(ir, props, _mk_search_result(),
                                         minimized_inputs={"b0|('b0', 'range_bound', '|range bound violated')": ["2h"]})
    rep = report_mod.build_report(ir, props, _mk_search_result(), findings,
                                  emitted_tests={"examples": "/tmp/x.py"})
    d = rep.to_dict()
    for key in ("target", "coverage", "properties_checked", "bugs", "meta"):
        assert key in d
    assert d["coverage"]["branch"] == 0.875
    bug = d["bugs"][0]
    for key in ("id", "severity_class", "minimal_input", "invariant_violated",
                "expected", "actual", "root_cause_guess", "suggested_patch"):
        assert key in bug, f"missing {key}"
    assert bug["severity_class"] in ("CONFIRMED", "SUSPECTED")
    assert bug["minimal_input"].startswith("(")


def test_write_artifacts_and_md_sections(tmp_path):
    ir = ir_of("samples/slugify.py::slugify")
    props = [Property(name="ec", kind="exception_contract", description="never raises",
                      params={}, provenance="template")]
    sres = SearchResult()
    sres.findings = [RawFinding(args=["a", "\\"], kwargs={},
                                violation={"property": "ec", "kind": "exception_contract",
                                           "expected": "no undeclared exception",
                                           "actual": "re.error: ...", "provenance": "template"},
                                exec_result=ExecResult(status="EXCEPTION",
                                                       exc_type="re.error"))]
    sres.executions, sres.budget_used_s = 10, 1.0
    findings = report_mod.build_findings(ir, props, sres)
    assert findings[0].severity_class == "CONFIRMED"
    rep = report_mod.build_report(ir, props, sres, findings, {})
    paths = report_mod.write_report(rep, str(tmp_path))
    loaded = json.load(open(paths["json"]))
    assert loaded["bugs"][0]["severity_class"] == "CONFIRMED"
    md = open(paths["md"]).read()
    for section in ("# SENTINEL report", "## Findings", "## Spec contract"):
        assert section in md
