"""Orchestrator: end-to-end scan pipeline over one target."""
from __future__ import annotations

import json
import os
import time

from . import ingest, report as report_mod, search as search_mod, spec as spec_mod, synth
from .llm import LLMClient
from .minimize import minimize_finding
from .models import Property
from .runner import Runner


def capture_reference_cases(reference_spec: str, seeds: list, timeout: float = 2.0) -> list:
    """Capture expected behaviors from a user-supplied reference implementation."""
    sel = ingest.parse_selection(reference_spec)
    ref_ir = [t for t in ingest.build_target_ir(sel) if not t.skip_reason][0]
    runner = Runner(ref_ir.module_path, ref_ir.qualname,
                    param_names=ref_ir.param_names, per_call_timeout=timeout)
    try:
        cases = []
        for s in seeds[:24]:
            res = runner.execute(list(s), {})
            expect = "<EXC %s>" % res.exc_type if res.status == "EXCEPTION" else res.value_repr
            cases.append({"args": list(s), "expect_repr": expect})
        return cases
    finally:
        runner.close()


def scan_target(selection_spec: str, out_dir: str | None = None, budget_s: float = 60.0,
                max_executions: int = 1500, per_call_timeout: float = 2.0,
                seed: int | None = None, use_llm: bool = False,
                emit_tests_dir: str | None = None,
                snapshot_reference: bool = False,
                reference_spec: str | None = None,
                session_log: str | None = None) -> tuple[report_mod.ScanReport, dict]:
    sel = ingest.parse_selection(selection_spec)
    targets = ingest.build_target_ir(sel)
    runnable = [t for t in targets if not t.skip_reason]
    if not runnable:
        raise RuntimeError(f"no runnable targets: {[t.skip_reason for t in targets]}")
    ir = runnable[0]

    llm_client = LLMClient() if use_llm else None
    properties = spec_mod.infer_spec(ir, llm_client=llm_client)

    runner = Runner(ir.module_path, ir.qualname, param_names=ir.param_names,
                    per_call_timeout=per_call_timeout)
    try:
        snap_prop = None
        if reference_spec:
            seeds = synth.seed_inputs(ir, cap=40)
            cases = capture_reference_cases(reference_spec, seeds, per_call_timeout)
            snap_prop = Property(name="differential_vs_reference", kind="snapshot",
                                 description=f"behavior matches reference {reference_spec}",
                                 params={"cases": cases},
                                 provenance="snapshot", confidence=1.0)
            sres = search_mod.run_search(
                ir, properties, runner, budget_s=budget_s, max_executions=max_executions,
                seed=seed, snapshot_prop=snap_prop, snapshot_cases=cases)
        else:
            sres = search_mod.run_search(
                ir, properties, runner, budget_s=budget_s, max_executions=max_executions,
                seed=seed, snapshot_prop=snap_prop)

        minimized = {}
        for rf in sres.findings:
            key = rf.violation.get("property", "") + "|" + str(rf.dedup_key)
            if rf.violation.get("kind") == "snapshot":
                # violation belongs to a fixed reference case, not the input
                minimized[key] = rf.violation.get("case_args") or rf.args
                continue
            mini, used = minimize_finding(runner, ir, properties + (
                [snap_prop] if snap_prop else []),
                rf.args, rf.violation.get("property", "exception_contract"),
                max_execs=200)
            minimized[key] = mini
            sres.executions += used

        findings = report_mod.build_findings(ir, properties + (
            [snap_prop] if snap_prop else []), sres, minimized_inputs=minimized,
            llm_explainer=(lambda i, d: llm_client.explain_finding(i, d))
            if (use_llm and llm_client.available) else None)

        emitted = {}
        if emit_tests_dir:
            emitted = synth.synthesize_all(ir, properties, emit_tests_dir)

        rep = report_mod.build_report(ir, properties, sres, findings, emitted)
        artifacts = {}
        if out_dir:
            artifacts = report_mod.write_report(rep, out_dir)
        if session_log:
            _log_session(session_log, selection_spec, rep)
        return rep, artifacts
    finally:
        runner.close()


def scan_all(spec: str, **kwargs) -> list[tuple[report_mod.ScanReport, dict]]:
    """Scan every top-level function in a file (skipping flagged ones)."""
    sel = ingest.parse_selection(spec)
    names, _ = ingest.ingest_file(sel.path)
    results = []
    for n in names:
        try:
            results.append(scan_target(f"{sel.path}::{n}", **kwargs))
        except RuntimeError:
            continue
    return results


def _log_session(path: str, spec: str, rep) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"spec": spec, "ts": time.time(),
                             "report": rep.to_dict()}, default=str) + "\n")


# ----------------------------------------------------------------------------------
# Auto-fix loop (spec §10 stretch): propose patch via LLM, verify it kills the
# finding without regressing other inputs, report FIXED / NOT_FIXED / REGRESSION.
# ----------------------------------------------------------------------------------

def _splice_function(module_source: str, ir, replacement: str) -> str:
    """Replace the target function's source segment with the proposed patch."""
    if ir.source_segment and ir.source_segment in module_source:
        return module_source.replace(ir.source_segment, replacement, 1)
    return module_source + "\n\n" + replacement


def fix_findings(selection_spec: str, out_dir: str | None = None,
                 budget_s: float = 25.0, max_executions: int = 500,
                 per_call_timeout: float = 2.0, seed: int | None = None,
                 llm_client=None) -> list[dict]:
    """Scan, then attempt an LLM-proposed patch per finding; verify each."""
    if llm_client is None or not getattr(llm_client, "available", False):
        raise RuntimeError("fix requires the LLM tier (set OPENROUTER_API_KEY, pass --llm)")

    sel = ingest.parse_selection(selection_spec)
    ir = [t for t in ingest.build_target_ir(sel) if not t.skip_reason][0]
    properties = spec_mod.infer_spec(ir)
    props_dicts = [p.to_dict() for p in properties]

    orig_runner = Runner(ir.module_path, ir.qualname, param_names=ir.param_names,
                         per_call_timeout=per_call_timeout)
    try:
        # findings on the original
        sres = search_mod.run_search(ir, properties, orig_runner,
                                     budget_s=budget_s, max_executions=max_executions,
                                     seed=seed)
        minimized = {}
        for rf in sres.findings:
            key = rf.violation.get("property", "") + "|" + str(rf.dedup_key)
            mini, used = minimize_finding(orig_runner, ir, properties, rf.args,
                                          rf.violation.get("property",
                                                           "exception_contract"),
                                          max_execs=120)
            minimized[key] = mini

        # regression baseline: original behavior on seeds + which seeds already violate
        seeds = synth.seed_inputs(ir, cap=24)
        orig_behavior = {}
        violating_seeds = set()
        for s in seeds:
            res = orig_runner.execute(s, {})
            orig_behavior[repr(s)] = ("<EXC %s>" % res.exc_type
                                      if res.status == "EXCEPTION" else res.value_repr)
            _, viols = orig_runner.check(s, {}, props_dicts)
            if viols or res.status == "TIMEOUT":
                violating_seeds.add(repr(s))
    finally:
        orig_runner.close()

    results = []
    for rf in sres.findings:
        pname = rf.violation.get("property", "")
        key = pname + "|" + str(rf.dedup_key)
        minimal_args = minimized.get(key, rf.args)
        finding_desc = {"property": pname, "kind": rf.violation.get("kind"),
                        "input": minimal_args, "expected": rf.violation.get("expected"),
                        "actual": rf.violation.get("actual"),
                        "source": ir.source_segment}
        patch = llm_client.propose_patch(ir, finding_desc)
        entry = {"finding": f"{pname} on {minimal_args!r}", "patch_proposed": bool(patch)}
        if not patch:
            entry["status"] = "NO_PATCH"
            results.append(entry)
            continue

        patched_source = _splice_function(
            open(ir.module_path, encoding="utf-8").read(), ir, patch)
        import tempfile as _tf
        tmp = _tf.mkdtemp(prefix="sentinel_fix_")
        stem = os.path.splitext(os.path.basename(ir.module_path))[0]
        patched_path = os.path.join(tmp, stem + ".py")
        with open(patched_path, "w", encoding="utf-8") as fh:
            fh.write(patched_source)

        try:
            patched_ir = ingest.build_target_ir(
                ingest.parse_selection(f"{patched_path}::{ir.qualname}"))[0]
            fix_runner = Runner(patched_path, ir.qualname,
                                param_names=patched_ir.param_names,
                                per_call_timeout=per_call_timeout)
            new_violations = 0
            behavior_changes = 0
            try:
                # A) does the minimal repro still violate?
                chosen = [p.to_dict() for p in properties if p.name == pname] or props_dicts
                res, viols = fix_runner.check(list(minimal_args), {}, chosen)
                still_broken = bool(viols) or res.status == "TIMEOUT"

                # B) regression scan on seeds:
                #    - REGRESSION = patched violates properties where original did not
                #    - plain behavior changes on previously-passing seeds are reported
                #      as `behavior_changes` for human review (they are expected when
                #      a patch fixes sibling inputs of the same defect)
                for s in seeds:
                    res2, viols2 = fix_runner.check(s, {}, props_dicts)
                    newly_bad = bool(viols2) or res2.status == "TIMEOUT"
                    now = ("<EXC %s>" % res2.exc_type
                           if res2.status == "EXCEPTION" else res2.value_repr)
                    changed = repr(s) not in violating_seeds and                         now != orig_behavior[repr(s)]
                    if newly_bad and repr(s) not in violating_seeds:
                        new_violations += 1
                    elif changed:
                        behavior_changes += 1
            finally:
                fix_runner.close()

            if still_broken:
                entry["status"] = "NOT_FIXED"
            elif new_violations:
                entry["status"] = f"REGRESSION({new_violations})"
                entry["behavior_changes"] = behavior_changes
            else:
                entry["status"] = "FIXED"
                entry["behavior_changes"] = behavior_changes
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                    patch_file = os.path.join(
                        out_dir, f"{ir.qualname.replace('.', '_')}_suggested_patch.py")
                    with open(patch_file, "w", encoding="utf-8") as fh:
                        fh.write(patch)
                    entry["patch_file"] = patch_file
        except Exception as e:
            entry["status"] = f"PATCH_INVALID({type(e).__name__})"
        finally:
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)
        results.append(entry)
    return results
