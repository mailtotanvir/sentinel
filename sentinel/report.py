"""Bug Reporter: ScanReport assembly + report.json / report.md emitters."""
from __future__ import annotations

import datetime
import json
import os

from .models import Finding, Property, ScanReport, TargetIR
from .search import CONFIRMED_KINDS, RawFinding, SearchResult

SENTINEL_VERSION = "0.1.0"


def classify(violation_kind: str, exec_status: str) -> str:
    if violation_kind in CONFIRMED_KINDS or exec_status == "TIMEOUT":
        return "CONFIRMED"
    return "SUSPECTED"


# Container annotations that declare no element structure: crashes triggered by
# shape-mismatched elements inside such params are spec-ambiguous (DESIGN section 11
# mitigation: severity downgrade instead of a hard CONFIRMED).
_UNSTRUCTURED_CONTAINERS = {"", "any", "object", "list", "dict", "mapping",
                            "sequence", "iterable", "collection", "tuple"}


def _unstructured_container_params(ir: TargetIR) -> set:
    out = set()
    for p in ir.params:
        a = (p.annotation or "").strip().replace("typing.", "").lower()
        if "[" in a or "|" in a:
            continue  # element structure declared
        if a in _UNSTRUCTURED_CONTAINERS:
            out.add(p.name)
    return out


def _garbage_shaped_input(ir: TargetIR, args) -> bool:
    """True when some unstructured container param received non-sequence elements."""
    unstructured = _unstructured_container_params(ir)
    names = [p.name for p in ir.params]
    for name, val in zip(names, args):
        if name not in unstructured or not isinstance(val, list):
            continue
        if val and any(not isinstance(e, (list, tuple)) for e in val):
            return True
    return False


def _root_cause_static(ir: TargetIR, rf: RawFinding) -> tuple[str, str]:
    """Static heuristic root-cause guess + patch suggestion."""
    tail = rf.exec_result.traceback_tail or []
    line_hint = ""
    for ln in reversed(tail):
        if 'File "<string>"' in ln or ".py" in ln and "line" in ln:
            continue
        if ln.strip() and not ln.strip().startswith("File"):
            line_hint = ln.strip()
            break
    kind = rf.violation.get("kind")
    if exec_is_timeout(rf):
        cause = "target fails to terminate for this input (possible unbounded loop)"
        fix = "add a termination condition or input validation guarding this path"
    elif kind == "exception_contract":
        cause = f"unhandled {rf.exec_result.exc_type} escapes on this input path"
        fix = ("validate inputs before use, or catch and convert to a declared exception"
               + (f" (last statement near: {line_hint[:80]})" if line_hint else ""))
    elif kind == "range_bound":
        cause = f"computed result violates documented contract ({rf.violation.get('expected')})"
        fix = "review arithmetic/branch handling for boundary values in this input class"
    elif kind == "snapshot":
        cause = "behavior diverges from reference snapshot on this input"
        fix = "diff against reference implementation for this branch"
    elif kind == "determinism":
        cause = "repeated calls with identical input produce different results"
        fix = "remove hidden state / randomness dependence"
    else:
        cause = rf.violation.get("detail", "property violated")
        fix = "inspect the code path exercised by this input"
    return cause, fix


def exec_is_timeout(rf: RawFinding) -> bool:
    return rf.exec_result.status == "TIMEOUT" or \
        rf.violation.get("kind") == "exception_contract" and \
        rf.exec_result.status in ("TIMEOUT",)


def build_findings(ir: TargetIR, properties: list[Property], search_result: SearchResult,
                   minimized_inputs: dict | None = None,
                   llm_explainer=None) -> list[Finding]:
    minimized_inputs = minimized_inputs or {}
    findings: list[Finding] = []
    for i, rf in enumerate(sorted(search_result.findings,
                                  key=lambda f: 0 if classify(f.violation.get("kind", ""),
                                                              f.exec_result.status) == "CONFIRMED"
                                  else 1)):
        pname = rf.violation.get("property") or "unknown_property"
        sev = classify(rf.violation.get("kind", ""), rf.exec_result.status)
        shape_note = ""
        if sev == "CONFIRMED" and rf.violation.get("kind") == "exception_contract" \
                and _garbage_shaped_input(ir, rf.args):
            sev = "SUSPECTED"
            shape_note = (" (input violates the parameter's unstated element "
                          "shape; spec ambiguity -> SUSPECTED)")
        if rf.violation.get("kind") == "snapshot" and rf.violation.get("case_args"):
            minimal = rf.violation["case_args"]
        else:
            minimal = minimized_inputs.get(pname + "|" + rf.dedup_key.__str__(), rf.args)
        cause, fix = _root_cause_static(ir, rf)
        if shape_note:
            cause += shape_note
        if llm_explainer is not None:
            upgraded = llm_explainer(ir, {
                "input": minimal, "violation": rf.violation,
                "status": rf.exec_result.status,
                "traceback_tail": rf.exec_result.traceback_tail})
            if upgraded:
                cause = upgraded
        try:
            orig_len = len(repr_list(rf.args))
            min_len = len(repr_list(minimal))
            ratio = round(min_len / orig_len, 3) if orig_len else 1.0
        except Exception:
            ratio = 1.0
        findings.append(Finding(
            id=f"BUG-{i + 1:03d}",
            severity_class=sev,
            minimal_input=repr_list(minimal),
            minimality_ratio=ratio,
            invariant_violated=pname,
            expected=str(rf.violation.get("expected", "")),
            actual=str(rf.violation.get("actual", rf.exec_result.value_repr or "")),
            root_cause_guess=cause,
            suggested_patch=fix,
            provenance=str(rf.violation.get("provenance", "")),
            original_input=repr_list(rf.args),
        ))
    return findings


def repr_list(vals) -> str:
    try:
        return "(" + ", ".join(repr(v) for v in vals) + ")"
    except Exception:
        return "<unrepr-able>"


def build_report(ir: TargetIR, properties: list[Property], search_result: SearchResult,
                 findings: list[Finding], emitted_tests: dict, extra_meta: dict | None = None) -> ScanReport:
    meta = {
        "sentinel_version": SENTINEL_VERSION,
        "budget_used_s": search_result.budget_used_s,
        "executions": search_result.executions,
        "skipped_reasons": ([ir.skip_reason] if ir.skip_reason else []),
        "emitted_tests": {k: v for k, v in emitted_tests.items() if v},
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if extra_meta:
        meta.update(extra_meta)
    rep = ScanReport(
        target=ir.callable_label,
        coverage={
            "branch": search_result.coverage.get("branch_pct", 0.0),
            "line": search_result.coverage.get("line_pct", 0.0),
        },
        properties_checked=[{
            "name": p.name, "kind": p.kind, "provenance": p.provenance,
            "confidence": p.confidence, "description": p.description} for p in properties],
        bugs=[f.to_dict() for f in findings],
        meta=meta,
    )
    if findings:
        ratios = [f.minimality_ratio for f in findings]
        rep.meta["minimality"] = {
            "mean_ratio": round(sum(ratios) / len(ratios), 3),
            "worst_ratio": max(ratios),
        }
    return rep


def write_report(report: ScanReport, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "report.json")
    md_path = os.path.join(out_dir, "report.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    return {"json": json_path, "md": md_path}


def render_markdown(report: ScanReport) -> str:
    lines = [f"# SENTINEL report — `{report.target}`", ""]
    cov = report.coverage
    lines.append(f"- Branch coverage: **{cov.get('branch', 0) * 100:.1f}%** | "
                 f"Line coverage: **{cov.get('line', 0) * 100:.1f}%**")
    lines.append(f"- Executions: {report.meta.get('executions')} | "
                 f"Budget used: {report.meta.get('budget_used_s')}s")
    lines.append(f"- Properties checked: {len(report.properties_checked)} "
                 f"({sum(1 for p in report.properties_checked if p['provenance'] == 'llm')} via LLM)")
    bugs = report.bugs
    confirmed = [b for b in bugs if b["severity_class"] == "CONFIRMED"]
    suspected = [b for b in bugs if b["severity_class"] == "SUSPECTED"]
    lines += ["", f"## Findings — {len(confirmed)} CONFIRMED, {len(suspected)} SUSPECTED", ""]
    if not bugs:
        lines.append("_No property violations found within budget._")
    for b in bugs:
        lines.append(f"### {b['id']} [{b['severity_class']}] — violates `{b['invariant_violated']}`")
        lines.append(f"- Input: `{b['minimal_input']}`")
        lines.append(f"- Expected: {b['expected']}")
        lines.append(f"- Actual: {b['actual']}")
        if "minimality_ratio" in b:
            lines.append(f"- Minimality: {b['minimality_ratio']:.0%} of original input size")
        if b.get("root_cause_guess"):
            lines.append(f"- Root cause guess: {b['root_cause_guess']}")
        if b.get("suggested_patch"):
            lines.append(f"- Suggested fix: {b['suggested_patch']}")
        lines.append("")
    props_table = ["| property | kind | provenance | confidence |",
                   "|---|---|---|---|"]
    for p in report.properties_checked:
        props_table.append(f"| `{p['name']}` | {p['kind']} | {p['provenance']} | {p['confidence']} |")
    lines += ["## Spec contract"] + props_table
    return "\n".join(lines) + "\n"
