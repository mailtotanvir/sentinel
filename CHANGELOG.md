# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/) (advisory).

## [0.1.0] - 2026-08-24

First public release. Complete v1 scope per DESIGN.md, plus three hardening sprints.

### Added
- Pipeline: AST ingest → spec inference → seed/test synthesis → coverage-guided
  adversarial search → ddmin minimization → JSON+Markdown reports.
- Sandboxed worker subprocesses: line-JSON IPC, per-call SIGALRM timeouts,
  process-group kill with orphan protection, rlimits, stdout-shielded framing.
- Spec inference: docstring mining (Google/NumPy/reST), name priors, exception audit,
  property templates, `.sentinelspec.py` overrides.
- Search: annotation-aware edge lattice seeds, docstring example mining, coordinated
  cross-parameter families, structure-preserving pair-list mutators, joint two-parameter
  probes, branch-arc corpus with novelty energy, determinism guard on findings.
- Property kinds: exception contract, range bounds, determinism, idempotence,
  round-trip, user metamorphic relations, snapshot differential oracle.
- Minimizer: hypothesis shrinking when available + type-aware ddmin fallback;
  per-finding minimality metric in reports.
- CONFIRMED vs SUSPECTED severity model; garbage-shape inputs on structure-less
  container params downgrade to SUSPECTED (spec-ambiguity policy).
- Mutation engine: statement deletion, compare/binop/augassign flips, slice
  off-by-one, string mutations; docstrings and exception-message text excluded as
  unobservable (equivalent-mutant hygiene).
- Benchmark harness: multi-seed mean/min/max reporting, disjoint holdout split for
  overfitting checks, naive-LLM baseline (validity-gated) via opencode CLI or direct
  REST with temperature=0, rate-limit-aware status recording.
- LLM tiers (optional, untrusted): spec suggestions validated before use, root-cause
  explanations, auto-fix proposals verified against findings + regression scan.
  Providers: OpenRouter (`stealth/ox-alpha`) with Gemini (`gemini-3.6-flash`) fallback.
- CLI: `scan` (--probe/--all/--reference/--emit-tests/--json/--llm), `bench`,
  `fix`, `replay`; exit-code contract 0/1/2; session logging.
- CI: lint, tests with coverage, sandboxed end-to-end smoke scan.

### Verified
- 66-test suite green; benchmark campaign: SENTINEL 32/39 mutants caught (82%,
  zero variance across 3 seeds), holdout split 35/42 (83%), zero true false positives;
  fair naive-baseline comparison recorded in results_baseline/RESULTS.md.
