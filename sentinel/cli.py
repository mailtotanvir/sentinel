"""SENTINEL command-line interface."""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel",
                                     description="SENTINEL - adversarial verification agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="scan a function/module for bugs")
    p_scan.add_argument("target", help="<file.py::func> or <file.py>")
    p_scan.add_argument("--out", default=None, help="output directory for report artifacts")
    p_scan.add_argument("--budget", type=float, default=60.0, help="wall-clock budget seconds")
    p_scan.add_argument("--max-executions", type=int, default=1500)
    p_scan.add_argument("--timeout", type=float, default=2.0, help="per-call timeout seconds")
    p_scan.add_argument("--seed", type=int, default=None)
    p_scan.add_argument("--llm", action="store_true", help="enable LLM spec tier (OpenRouter)")
    p_scan.add_argument("--emit-tests", default=None, help="directory for generated tests")
    p_scan.add_argument("--probe", default=None,
                        help="manual mode: python-literal args tuple, e.g. \"('2h',)\"")
    p_scan.add_argument("--reference", default=None,
                        help="differential mode: <ref.py::func> to compare behavior against")
    p_scan.add_argument("--all", action="store_true",
                        help="scan every top-level function in the target file")
    p_scan.add_argument("--json", action="store_true", help="print JSON report to stdout")

    p_bench = sub.add_parser("bench", help="benchmark vs mutants and naive baseline")
    p_bench.add_argument("samples", help="samples directory or single file")
    p_bench.add_argument("--out", default="results")
    p_bench.add_argument("--baseline", action="store_true",
                         help="also run opencode CLI baseline (stealth/ox-alpha)")
    p_bench.add_argument("--baseline-rest", action="store_true",
                         help="with --baseline: call LLM providers directly with "
                              "temperature=0 instead of the opencode CLI "
                              "(OpenRouter first, Gemini fallback)")
    p_bench.add_argument("--baseline-only", action="store_true",
                         help="skip SENTINEL mutant evaluation and only run the "
                              "baseline side (requires --baseline)")
    p_bench.add_argument("--merge-from", default=None,
                         help="with --baseline-only: bench_details.json from a "
                              "previous full run to reuse SENTINEL results from")
    p_bench.add_argument("--budget", type=float, default=25.0)
    p_bench.add_argument("--max-executions", type=int, default=600)
    p_bench.add_argument("--seed", type=int, default=7)
    p_bench.add_argument("--seeds", type=int, default=1,
                         help="run the SENTINEL side over N deterministic seeds and "
                              "report mean/min/max catch rate (DESIGN section 8)")
    p_bench.add_argument("--holdout-seed", type=int, default=None,
                         help="also evaluate a disjoint mutant set from this seed "
                              "(overfitting check, spec section 11)")

    p_fix = sub.add_parser("fix", help="LLM-proposed patches, verified against findings")
    p_fix.add_argument("target", help="<file.py::func>")
    p_fix.add_argument("--out", default=None)
    p_fix.add_argument("--budget", type=float, default=20.0)
    p_fix.add_argument("--max-executions", type=int, default=400)
    p_fix.add_argument("--seed", type=int, default=None)

    p_replay = sub.add_parser("replay", help="replay a session log")
    p_replay.add_argument("session")

    args = parser.parse_args(argv)

    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "bench":
        from .bench import cmd_bench
        return cmd_bench(args)
    if args.cmd == "fix":
        return cmd_fix(args)
    if args.cmd == "replay":
        return cmd_replay(args)
    return 2


def cmd_scan(args) -> int:
    from .orchestrator import scan_target

    if args.probe is not None:
        return cmd_probe(args)

    if args.all:
        return cmd_scan_all(args)

    rep, artifacts = scan_target(
        args.target, out_dir=args.out, budget_s=args.budget,
        max_executions=args.max_executions, per_call_timeout=args.timeout,
        seed=args.seed, use_llm=args.llm, emit_tests_dir=args.emit_tests,
        reference_spec=args.reference,
        session_log=os.path.join(".sentinel", "sessions.jsonl"))

    if args.json:
        print(json.dumps(rep.to_dict(), indent=2))
    else:
        md = rep.to_dict()
        print(f"target   : {md['target']}")
        print(f"coverage : branch {md['coverage']['branch'] * 100:.1f}% "
              f"| line {md['coverage']['line'] * 100:.1f}%")
        print(f"execs    : {md['meta']['executions']} in {md['meta']['budget_used_s']}s")
        conf = [b for b in md['bugs'] if b['severity_class'] == 'CONFIRMED']
        susp = [b for b in md['bugs'] if b['severity_class'] == 'SUSPECTED']
        print(f"bugs     : {len(conf)} CONFIRMED, {len(susp)} SUSPECTED")
        for b in md['bugs']:
            print(f"  - {b['id']} [{b['severity_class']}] input={b['minimal_input']} "
                  f"violates={b['invariant_violated']} expected={b['expected']!r} "
                  f"actual={b['actual']!r}")
        for label, path in artifacts.items():
            print(f"artifact : {label} -> {path}")
    has_bugs = any(b["severity_class"] == "CONFIRMED" for b in rep.bugs) or bool(rep.bugs)
    return 1 if has_bugs else 0


def cmd_scan_all(args) -> int:
    from .ingest import ingest_file, parse_selection
    from .orchestrator import scan_target
    sel = parse_selection(args.target)
    names, _ = ingest_file(sel.path)
    out_dir = args.out or ".sentinel"
    any_bugs = False
    reports = []
    for n in names:
        tgt = f"{sel.path}::{n}"
        out = os.path.join(out_dir, n)
        try:
            rep, artifacts = scan_target(
                tgt, out_dir=out, budget_s=args.budget,
                max_executions=args.max_executions, per_call_timeout=args.timeout,
                seed=args.seed, use_llm=args.llm, reference_spec=args.reference)
        except RuntimeError as e:
            print(f"skip {n}: {e}")
            continue
        reports.append(rep)
        n_conf = sum(1 for b in rep.bugs if b["severity_class"] == "CONFIRMED")
        n_susp = len(rep.bugs) - n_conf
        print(f"{n}: {n_conf} CONFIRMED, {n_susp} SUSPECTED "
              f"(branch {rep.coverage.get('branch', 0) * 100:.0f}%) -> {out}")
        if rep.bugs:
            any_bugs = True
    _write_scan_summary(out_dir, sel.path, reports)
    return 1 if any_bugs else 0


def _write_scan_summary(out_dir: str, source_path: str, reports: list) -> str:
    from datetime import datetime

    import json as _json
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "source": source_path,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "targets": [r.to_dict() for r in reports],
    }
    json_path = os.path.join(out_dir, "summary.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        _json.dump(payload, fh, indent=2)
    lines = ["# SENTINEL module scan — " + source_path, "",
             f"- Generated: {payload['generated_at']}",
             f"- Targets scanned: {len(reports)}", "",
             "| target | branch cov | CONFIRMED | SUSPECTED | first finding |",
             "|---|---|---|---|---|"]
    for r in reports:
        d = r.to_dict()
        conf = sum(1 for b in d["bugs"] if b["severity_class"] == "CONFIRMED")
        susp = len(d["bugs"]) - conf
        first = d["bugs"][0]["minimal_input"] if d["bugs"] else "-"
        name = d["target"].split("::")[-1]
        lines.append(f"| `{name}` | {d['coverage']['branch'] * 100:.0f}% | "
                     f"{conf} | {susp} | `{first}` |")
    md_path = os.path.join(out_dir, "summary.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return json_path


def cmd_probe(args) -> int:
    from .ingest import parse_selection, build_target_ir
    from .runner import Runner
    sel = parse_selection(args.target)
    irs = [t for t in build_target_ir(sel) if not t.skip_reason]
    if not irs:
        print("no runnable target", file=sys.stderr)
        return 2
    ir = irs[0]
    call_args = ast.literal_eval(args.probe)
    if not isinstance(call_args, tuple):
        call_args = (call_args,)
    r = Runner(ir.module_path, ir.qualname, param_names=ir.param_names,
               per_call_timeout=args.timeout)
    try:
        res = r.execute(list(call_args), {})
        print(f"signature: def {ir.qualname}{ir.signature}")
        print(f"status   : {res.status}")
        if res.status == "OK":
            print(f"value    : {res.value_repr}")
        elif res.status == "EXCEPTION":
            print(f"exception: {res.exc_type}: {res.exc_repr}")
        else:
            print(f"detail   : {res.exc_repr}")
        return 0 if res.ok else 1
    finally:
        r.close()


def cmd_fix(args) -> int:
    from .llm import LLMClient
    from .orchestrator import fix_findings
    client = LLMClient()
    if not client.available:
        print("sentinel fix requires OPENROUTER_API_KEY in the environment", file=sys.stderr)
        return 2
    results = fix_findings(args.target, out_dir=args.out, budget_s=args.budget,
                           max_executions=args.max_executions, seed=args.seed,
                           llm_client=client)
    print(f"{'status':<22} finding")
    fixed = 0
    for r in results:
        print(f"{r['status']:<22} {r['finding'][:80]}")
        if r["status"] == "FIXED":
            fixed += 1
        if r.get("patch_file"):
            print(f"  patch saved: {r['patch_file']}")
    print(f"\n{fixed}/{len(results)} findings fixed with verified patches")
    return 0 if fixed else 1


def cmd_replay(args) -> int:
    with open(args.session, encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    print(f"session contains {len(lines)} scans")
    for entry in lines:
        r = entry["report"]
        n_conf = sum(1 for b in r["bugs"] if b["severity_class"] == "CONFIRMED")
        print(f"- {entry['spec']}: {len(r['bugs'])} findings ({n_conf} confirmed), "
              f"branch={r['coverage'].get('branch', 0):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
