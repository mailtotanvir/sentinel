"""Benchmark: SENTINEL vs mutants, optionally vs naive-prompt baseline (opencode CLI)."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import tempfile

from . import mutants as mut_engine
from .baseline import generate_tests, run_pytest
from .ingest import build_target_ir, ingest_file, parse_selection
from .models import Property
from .runner import Runner
from .search import run_search
from .spec import infer_spec
from . import report as report_mod


def _sample_files(samples_path: str) -> list[str]:
    if os.path.isfile(samples_path):
        return [samples_path]
    out = []
    for f in sorted(os.listdir(samples_path)):
        if f.endswith(".py") and not f.startswith("_") and f != "conftest.py":
            out.append(os.path.join(samples_path, f))
    return out


def scan_original_properties(path: str, qualname: str, budget_s: float,
                             max_executions: int, seed: int):
    """Scan the ORIGINAL to build properties + snapshot + original findings (FP set)."""
    ir = build_target_ir(parse_selection(f"{path}::{qualname}"))[0]
    props = infer_spec(ir)
    runner = Runner(ir.module_path, ir.qualname, param_names=ir.param_names)
    try:
        snap = Property(name="snapshot_vs_reference", kind="snapshot",
                        description="behavior matches original reference",
                        provenance="snapshot", confidence=0.9)
        sres = run_search(ir, props, runner, budget_s=budget_s,
                          max_executions=max_executions, seed=seed,
                          snapshot_prop=snap)

        def sev_of(f):
            sev = report_mod.classify(f.violation.get("kind", ""),
                                      f.exec_result.status)
            if sev == "CONFIRMED" and f.violation.get("kind") == "exception_contract" \
                    and report_mod._garbage_shaped_input(ir, f.args):
                sev = "SUSPECTED"
            return sev

        original_findings = [
            {"args": list(f.args), "kwargs": dict(f.kwargs),
             "property": f.violation.get("property"),
             "kind": f.violation.get("kind"),
             "severity": sev_of(f)}
            for f in sres.findings]
        return ir, props, snap, sres.coverage, original_findings
    finally:
        runner.close()


def verify_mutant(mutant_source: str, orig_path: str, qualname: str, props,
                  snap: Property | None, budget_s: float, max_executions: int,
                  seed: int, tmp_dir: str):
    """Run search against a mutant module; returns findings + whether caught."""
    stem = os.path.splitext(os.path.basename(orig_path))[0]
    mpath = os.path.join(tmp_dir, stem + ".py")
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(mutant_source)
    ir = build_target_ir(parse_selection(f"{mpath}::{qualname}"))[0]
    runner = Runner(mpath, qualname, param_names=ir.param_names)
    caught = False
    n_findings = 0
    confirmed = 0
    try:
        sres = run_search(ir, [p for p in props], runner, budget_s=budget_s,
                          max_executions=max_executions, seed=seed)
        # rebind snapshot cases captured from original
        if snap is not None and snap.params.get("cases"):
            sp = Property(name=snap.name, kind=snap.kind,
                          description=snap.description,
                          params={"cases": snap.params["cases"]},
                          provenance=snap.provenance, confidence=1.0)
            sres2 = run_search(ir, [], runner, budget_s=min(budget_s, 15.0),
                               max_executions=min(max_executions // 2, 300),
                               seed=seed, snapshot_prop=sp,
                               snapshot_cases=snap.params["cases"])
            for rf in sres2.findings:
                sres.findings.append(rf)
        n_findings = len(sres.findings)
        confirmed = sum(1 for f in sres.findings
                        if f.violation.get("kind") == "exception_contract")
        for f in sres.findings:
            if not _reproduces_on_original(orig_path, qualname, f.args,
                                           f.violation):
                caught = True
                break
    finally:
        runner.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"caught": caught, "findings": n_findings, "confirmed": confirmed}


def _reproduces_on_original(orig_path: str, qualname: str, args, violation: dict) -> bool:
    """A finding counts only if it does NOT also fire on the original code.

    Snapshot findings are special: the oracle IS the original's recorded behavior,
    so a mismatch can never 'reproduce' on the original unless the original is
    itself nondeterministic (then we conservatively treat it as reproducing).
    """
    ir = build_target_ir(parse_selection(f"{orig_path}::{qualname}"))[0]
    runner = Runner(orig_path, qualname, param_names=ir.param_names)
    try:
        if violation.get("kind") == "snapshot":
            res = runner.execute(list(args), {})
            got = "<EXC %s>" % res.exc_type if res.status == "EXCEPTION" \
                else res.value_repr
            return got != violation.get("was")
        prop_name = violation.get("property")
        props = infer_spec(ir)
        chosen = [p.to_dict() for p in props
                  if p.name == prop_name] or \
                 [{"name": "exception_contract", "kind": "exception_contract",
                   "params": {}, "provenance": "template"}]
        res, viols = runner.check(list(args), {}, chosen)
        if viols:
            return True
        return res.status in ("TIMEOUT", "EXCEPTION", "WORKER_CRASH")
    except Exception:
        return True  # cannot verify -> conservative: treat as reproducing (not caught)
    finally:
        runner.close()


def cmd_bench(args: argparse.Namespace) -> int:
    files = _sample_files(args.samples)
    baseline_only = bool(getattr(args, "baseline_only", False))
    prev = {}
    if baseline_only and getattr(args, "merge_from", None):
        with open(args.merge_from, encoding="utf-8") as fh:
            prev = json.load(fh).get("per_sample", {})
    results = {}
    details = {"generated_at": datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds"),
        "seed": args.seed, "budget_s": args.budget,
        "max_executions": args.max_executions,
        "model": "stealth/ox-alpha",
        "gemini_model": os.environ.get("SENTINEL_GEMINI_MODEL", "gemini-3.6-flash"),
        "baseline_cli": "opencode" if args.baseline else None}
    os.makedirs(args.out, exist_ok=True)
    import time as _time

    for path in files:
        names, src = ingest_file(path)
        runnable = [n for n in names if not build_target_ir(
            parse_selection(f"{path}::{n}"))[0].skip_reason]
        for name in runnable:
            label = f"{os.path.basename(path)}::{name}"
            print(f"[bench] {label}", flush=True)
            import random as _random
            if getattr(args, "holdout_seed", None) is not None:
                # holdout mode: split mutant space into two disjoint halves
                all_u = mut_engine.unique_mutants(src)
                _random.Random(args.holdout_seed).shuffle(all_u)
                half = max(1, len(all_u) // 2)
                muts, pre_holdout = all_u[:half], all_u[half:]
            else:
                muts = mut_engine.generate_mutants(src, name, max_mutants=12,
                                                   seed=args.seed)
                pre_holdout = []
            if not muts:
                continue

            if baseline_only and label in prev and "per_seed" in prev[label]:
                # reuse SENTINEL-side results from a previous complete run
                results[label] = dict(prev[label])
                print("  [baseline-only] reused SENTINEL results from "
                      f"{args.merge_from}", flush=True)
            else:
                _run_sentinel_side(args, path, name, src, muts, pre_holdout,
                                   results, label)

            # --- baseline side ---
            if args.baseline:
                if baseline_only:
                    _time.sleep(float(os.environ.get(
                        "SENTINEL_BASELINE_PACING_S", "30")))
                base_res = _run_baseline(path, name, src, muts, args)
                results[label].update(base_res)
                print(f"  baseline: {base_res.get('baseline_caught', '?')}/"
                      f"{len(muts)} ({base_res.get('baseline_status', '')})",
                      flush=True)

    totals = _totals(results)
    _write_results_md(args, results, totals)
    from .baseline import BASELINE_PROVIDERS_USED
    with open(os.path.join(args.out, "bench_details.json"), "w") as fh:
        json.dump({**details,
                   "baseline_providers_used": sorted(set(BASELINE_PROVIDERS_USED)),
                   "per_sample": results}, fh, indent=2)
    print(_summary_text(results, totals))
    return 0


def _run_sentinel_side(args, path: str, name: str, src: str, muts, pre_holdout,
                       results: dict, label: str) -> None:
    """SENTINEL-side evaluation (multi-seed per DESIGN section 8)."""
    ir, props, snap, cov, orig_findings = scan_original_properties(
        path, name, args.budget, args.max_executions, args.seed)
    seeds_list = [args.seed] if args.seeds <= 1 else \
        [args.seed + 1000 * k for k in range(args.seeds)]
    per_seed = []
    for si, s in enumerate(seeds_list):
        sent_results = []
        for i, (desc, msrc) in enumerate(muts):
            try:
                r = verify_mutant(msrc, path, name, props, snap,
                                  min(args.budget, 20.0),
                                  min(args.max_executions, 500), s + i,
                                  tempfile.mkdtemp(prefix="sentinel_mut_"))
            except Exception as e:  # never let one mutant kill the bench
                r = {"caught": False, "findings": 0, "confirmed": 0,
                     "error": f"{type(e).__name__}: {e}"[:200]}
            sent_results.append({"desc": desc, **r})
            status = r.get("error") and "ERROR" or \
                ("CAUGHT" if r["caught"] else "survived")
            suffix = "" if len(seeds_list) == 1 else f" [seed {si + 1}]"
            print(f"  mutant {i + 1}/{len(muts)} {desc}: {status}{suffix}",
                  flush=True)
        per_seed.append({
            "seed": s,
            "caught": sum(1 for x in sent_results if x["caught"]),
            "detail": sent_results})
    caught_any_detail = per_seed[0]["detail"]
    for ps in per_seed[1:]:
        by_desc = {x["desc"]: x for x in ps["detail"]}
        for x in caught_any_detail:
            if not x.get("caught") and by_desc.get(x["desc"], {}).get("caught"):
                x["caught"] = True
                x["caught_in_later_seed"] = ps["seed"]
    entry = {
        "mutants": len(muts),
        "coverage_branch": cov.get("branch_pct", 0.0),
        "sentinel_caught": sum(1 for x in caught_any_detail if x["caught"]),
        "per_seed": [{"seed": p["seed"], "caught": p["caught"]} for p in per_seed],
        "confirmed_any_seed": max(
            sum(1 for x in p["detail"]
                if x.get("caught") and x.get("confirmed", 0) > 0)
            for p in per_seed),
        "original_findings_fp": len(orig_findings),
        "original_findings": orig_findings,
        "mutant_detail": caught_any_detail,
    }

    # --- holdout evaluation (overfitting check, spec section 11) ---
    if getattr(args, "holdout_seed", None) is not None:
        hold_muts = pre_holdout
        hold_results = []
        for i, (desc_h, msrc2) in enumerate(hold_muts):
            try:
                r2 = verify_mutant(msrc2, path, name, props, snap,
                                   min(args.budget, 20.0),
                                   min(args.max_executions, 500),
                                   args.holdout_seed + i,
                                   tempfile.mkdtemp(prefix="sentinel_hold_"))
            except Exception as e:
                r2 = {"caught": False, "findings": 0, "confirmed": 0,
                      "error": str(e)[:120]}
            hold_results.append({"desc": desc_h, **r2})
            print(f"  holdout {i + 1}/{len(hold_muts)} {desc_h}: "
                  f"{'CAUGHT' if r2['caught'] else 'survived'}", flush=True)
        entry["holdout"] = {
            "seed": args.holdout_seed,
            "mutants": len(hold_muts),
            "caught": sum(1 for x in hold_results if x["caught"]),
            "detail": hold_results,
        }
    results[label] = entry


def _run_baseline(path: str, name: str, src: str, muts, args) -> dict:
    stem = os.path.splitext(os.path.basename(path))[0]
    seg = build_target_ir(parse_selection(f"{path}::{name}"))[0].source_segment
    proj_orig = tempfile.mkdtemp(prefix="sentinel_base_orig_")
    shutil.copy(path, os.path.join(proj_orig, stem + ".py"))
    tests_code = generate_tests(name, seg, stem,
                                rest=bool(getattr(args, "baseline_rest", False)))
    if not tests_code:
        from .baseline import LAST_REST_ERROR
        status = "generation_failed"
        if "rate limited" in LAST_REST_ERROR or "429" in LAST_REST_ERROR:
            status = "rate_limited"
        shutil.rmtree(proj_orig, ignore_errors=True)
        return {"baseline_status": status}
    tpath = os.path.join(proj_orig, f"test_baseline_{stem}.py")
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write(tests_code)
    passed_orig, tail = run_pytest(proj_orig)
    if not passed_orig:
        shutil.rmtree(proj_orig, ignore_errors=True)
        return {"baseline_status": "invalid_tests_fail_on_original", "tail": tail[-200:]}

    caught = 0
    for i, (desc, msrc) in enumerate(muts):
        d = tempfile.mkdtemp(prefix=f"sentinel_base_m{i}_")
        shutil.copy(tpath, os.path.join(d, os.path.basename(tpath)))
        with open(os.path.join(d, stem + ".py"), "w", encoding="utf-8") as fh:
            fh.write(msrc)
        ok, _ = run_pytest(d)
        if not ok:
            caught += 1
        shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(proj_orig, ignore_errors=True)
    return {"baseline_status": "ok", "baseline_caught": caught}


def _totals(results: dict) -> dict:
    tot = {"mutants": 0, "sentinel_caught": 0}
    base_any = False
    for e in results.values():
        tot["mutants"] += e["mutants"]
        tot["sentinel_caught"] += e["sentinel_caught"]
        if "baseline_caught" in e:
            base_any = True
            tot.setdefault("baseline_caught", 0)
            tot["baseline_caught"] += e["baseline_caught"]
    tot["sentinel_rate"] = tot["sentinel_caught"] / max(tot["mutants"], 1)
    if base_any:
        tot["baseline_rate"] = tot.get("baseline_caught", 0) / max(tot["mutants"], 1)
    fp = sum(e.get("original_findings_fp", 0) for e in results.values())
    tot["original_findings_fp"] = fp
    tot["fp_confirmed"] = sum(
        1 for e in results.values() for f in e.get("original_findings", [])
        if f.get("severity") == "CONFIRMED")
    tot["fp_suspected"] = fp - tot["fp_confirmed"]
    # multi-seed stats (DESIGN section 8: mean + min/max over N seeds)
    n_seeds = max(len(e.get("per_seed") or []) for e in results.values())
    if n_seeds > 1:
        seed_rates = []
        for si in range(n_seeds):
            c = tm = 0
            for e in results.values():
                ps = e.get("per_seed") or []
                if si < len(ps):
                    c += ps[si]["caught"]
                    tm += e["mutants"]
            if tm:
                seed_rates.append(c / tm)
        if len(seed_rates) > 1:
            tot["seed_count"] = n_seeds
            tot["sentinel_rate_mean"] = sum(seed_rates) / len(seed_rates)
            tot["sentinel_rate_min"] = min(seed_rates)
            tot["sentinel_rate_max"] = max(seed_rates)
    h_tot = sum(e.get("holdout", {}).get("mutants", 0) for e in results.values())
    if h_tot:
        h_caught = sum(e.get("holdout", {}).get("caught", 0) for e in results.values())
        tot["holdout_mutants"] = h_tot
        tot["holdout_caught"] = h_caught
        tot["holdout_rate"] = h_caught / h_tot
    return tot


def _write_results_md(args, results, totals) -> None:
    n_seeds = getattr(args, "seeds", 1) or 1
    seed_note = (f"; seeds: {n_seeds} (per-seed stats below)"
                 if n_seeds > 1 else "")
    if getattr(args, "baseline_rest", False):
        from .baseline import BASELINE_PROVIDERS_USED
        provs = sorted(set(BASELINE_PROVIDERS_USED)) or ["none-succeeded"]
        base_harness = (f"direct REST temperature=0 "
                        f"(openrouter->gemini fallback; used: {', '.join(provs)})")
    else:
        base_harness = "opencode CLI"
    lines = ["# SENTINEL benchmark results", "",
             f"- Generated: {datetime.datetime.now().isoformat(timespec='seconds')}",
             "- Model/LLM tier + baseline model: `stealth/ox-alpha` via OpenRouter"
             " (LLM tier falls back to `gemini-3.6-flash`); "
             f"baseline harness: {base_harness}",
             f"- Seed: {args.seed}{seed_note}; per-mutant budget: <=20s / 500 execs", "",
             "| target | mutants | SENTINEL caught | rate | baseline caught | baseline rate |"
             " branch cov | FP on originals |",
             "|---|---|---|---|---|---|---|---|"]
    for label, e in results.items():
        brate = (f"{e.get('baseline_caught', 0)}/{e['mutants']}"
                 if "baseline_caught" in e else
                 (str(e.get("baseline_status")) if e.get("baseline_status") else "n/a"))
        brate_r = (f"{e['baseline_caught'] / e['mutants']:.0%}"
                   if "baseline_caught" in e else "n/a")
        ps = e.get("per_seed") or []
        if len(ps) > 1:
            rates = [p["caught"] / max(e["mutants"], 1) for p in ps]
            rate_cell = (f"{min(rates):.0%}-{max(rates):.0%} "
                         f"(mean {sum(rates) / len(rates):.0%})")
        else:
            rate_cell = f"{e['sentinel_caught'] / e['mutants']:.0%}"
        lines.append(
            f"| `{label}` | {e['mutants']} | {e['sentinel_caught']} | "
            f"{rate_cell} | {brate} | {brate_r} | "
            f"{e['coverage_branch'] * 100:.0f}% | {e['original_findings_fp']} |")
    lines += ["", f"**TOTAL**: SENTINEL {totals['sentinel_caught']}/{totals['mutants']} "
              f"({totals['sentinel_rate']:.0%})"
              + (f"; baseline {totals.get('baseline_caught', 0)}/{totals['mutants']} "
                 f"({totals['baseline_rate']:.0%})" if 'baseline_rate' in totals else "")
              + f"; findings on unmutated originals: {totals['original_findings_fp']} "
              f"(CONFIRMED {totals['fp_confirmed']} / SUSPECTED {totals['fp_suspected']}; "
              "SUSPECTED = spec-ambiguous garbage-shape inputs or the target's own "
              "seeded defect, see FP audit)",
              ""]
    if "sentinel_rate_mean" in totals:
        lines += [f"**Per-seed reproducibility** (DESIGN section 8, "
                  f"{totals['seed_count']} seeds): "
                  f"mean {totals['sentinel_rate_mean']:.0%}, "
                  f"min {totals['sentinel_rate_min']:.0%}, "
                  f"max {totals['sentinel_rate_max']:.0%}",
                  ""]
    if "holdout_rate" in totals:
        lines += [f"**Holdout eval** (disjoint mutants, seed={args.holdout_seed}): "
                  f"SENTINEL {totals['holdout_caught']}/{totals['holdout_mutants']} "
                  f"({totals['holdout_rate']:.0%}) - overfitting check per spec section 11",
                  ""]
    with open(os.path.join(args.out, "RESULTS.md"), "w") as fh:
        fh.write("\n".join(lines))


def _summary_text(results, totals) -> str:
    return (f"bench done: SENTINEL {totals['sentinel_caught']}/{totals['mutants']} mutants "
            f"caught ({totals['sentinel_rate']:.0%})"
            + (f" [per-seed mean {totals['sentinel_rate_mean']:.0%} "
               f"min {totals['sentinel_rate_min']:.0%} "
               f"max {totals['sentinel_rate_max']:.0%}]"
               if "sentinel_rate_mean" in totals else "")
            + (f", baseline {totals.get('baseline_caught', 0)}/{totals['mutants']}"
               if 'baseline_caught' in totals else ""))
