# SENTINEL — adversarial verification agent

SENTINEL hunts for bugs in Python functions. Give it a target (`file.py::func`) and it
infers a spec contract from docstrings, names and code structure, synthesizes edge-case
and property-based inputs, then runs a **coverage-guided adversarial search** inside
sandboxed worker processes — minimizing every finding to a small repro and classifying it
CONFIRMED (directly observed crash/timeout) or SUSPECTED (violated an inferred predicate).

Deterministic and offline by default; optional LLM tiers (spec suggestions, root-cause
explanations, auto-fix proposals) are untrusted plug-ins, never required.

```text
scan → infer spec → synthesize seeds/tests → coverage-guided search → minimize → report (json + md)
```

> ## ⚠️ Safety: the sandbox is NOT a security boundary
>
> Target functions execute in a separate process with wall-clock timeouts, CPU/memory
> rlimits and a scrubbed environment — enough isolation for *accidental* hangs, crashes
> and output pollution. It is **not** protection against deliberately malicious code:
> there is no seccomp/network/filesystem confinement. Only scan code you could safely
> run yourself, or wrap `sentinel` in a container/VM.

---

## Install

Python **3.11+**.

```bash
git clone https://github.com/<you>/sentinel-verifier.git
cd sentinel-verifier
pip install -e .[dev]          # pytest + hypothesis + pytest-cov
# LLM tiers are optional; set any of these to enable them:
export OPENROUTER_API_KEY=...  # stealth/ox-alpha via OpenRouter (spec tier / fixes)
export GEMINI_API_KEY=...      # gemini-3.6-flash fallback provider
```

## Quickstart

```bash
# manual probe of one call (sandboxed)
sentinel scan samples/parse_duration.py::parse_duration --probe "('2h',)"

# full adversarial scan -> .sentinel/<target>/report.json|md ; exit 1 if bugs found
sentinel scan samples/slugify.py::slugify --seed 7

# scan every top-level function in a file (+ summary.json/md)
sentinel scan samples/intervals.py --all

# differential mode: compare behavior against your own reference implementation
sentinel scan samples/intervals.py::merge_intervals \
    --reference samples/correct_intervals_ref.py::merge_intervals

# emit runnable edge-lattice + PBT test files for the target
sentinel scan samples/parse_duration.py::parse_duration --emit-tests emitted/

# benchmark against AST mutants (+ naive-LLM baseline), multi-seed + holdout split
python -m sentinel.cli bench samples --seeds 3 --holdout-seed 99 \
    --baseline --baseline-rest --out results --budget 18 --max-executions 350 --seed 7

# LLM-proposed patches, each verified against findings + regression scan
sentinel fix samples/parse_duration.py::parse_duration

# replay a recorded session log (.sentinel/sessions.jsonl)
sentinel replay .sentinel/sessions.jsonl
```

Exit codes: `0` no findings · `1` findings · `2` usage/target error.

## What the scanner checks

| property kind | meaning | provenance |
|---|---|---|
| `exception_contract` | only declared exceptions escape (crash-class → CONFIRMED) | Raises-section mining |
| `range_bound` | mined/documented result predicates hold | docstring/name priors, LLM (untrusted) |
| `deterministic` | f(x) == f(x) across calls | template |
| `idempotence` / `round_trip` | f(f(x)) == f(x) / inv(f(x)) == x | name-pairing priors |
| `metamorphic` | user-supplied transform + relation hold | user |
| `snapshot` | behavior matches a captured reference (differential oracle) | bench / `--reference` |

Findings are **CONFIRMED** only for directly observed defects (undeclared exceptions,
timeouts); everything inferred is **SUSPECTED**. Inputs that violate a parameter's unstated
element shape (e.g. `[0]` fed to a bare `list` param) are downgraded to SUSPECTED with an
explanatory note instead of inflating false positives.

## How it searches

1. Seeds from annotation-aware edge lattices, docstring example mining, and coordinated
   cross-parameter families (e.g. separator embedded next to whitespace).
2. Corpus-guided mutation with novelty-weighted energy: arithmetic/boundary flips,
   digit-aware string surgery, type-confusion injection, structure-preserving pair-list
   shifts, joint two-parameter probes.
3. Branch-arc feedback from `coverage.py` measured on the target module only; plateau
   detection stops early when no novel arcs and no pending findings.
4. Every candidate finding must reproduce twice more (determinism guard) before it counts.

## Benchmark evidence

Built-in deterministic AST mutation engine (statement deletion, compare/binop/augassign
flips, slice off-by-one, string mutations; docstrings and exception-message text excluded
as unobservable). Detection rule: a mutant is *caught* iff a finding reproduces on the
mutant but not on the original. Naive baseline = same model asked once to "write thorough
tests" (validity-gated: its suite must pass on the original).

Latest campaign (`results_holdout/RESULTS.md`, `results_baseline/RESULTS.md`):

| target | mutants | SENTINEL | naive baseline |
|---|---|---|---|
| clean reference targets | 13 | 11/13 (85%) | 11/13 (85%) |
| seeded-bug targets | 26 | 21/26 (81%) | invalid ×3 (suite fails on buggy original) |
| **total** | **39** | **32/39 (82%)**, per-seed 82/82/82% | 11/39 (28% raw) |

Holdout split (disjoint mutants): SENTINEL 35/42 (83%) — no overfitting gap.
False positives: zero true FPs across all campaigns (details in RESULTS.md audits).

Reproduce with the `bench` command under *Quickstart* (~35 min; needs at least one
LLM provider key for the baseline side only).

## Environment variables

| variable | purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter provider (model default `stealth/ox-alpha`) |
| `GEMINI_API_KEY` | Gemini fallback provider (`gemini-3.6-flash`) |
| `SENTINEL_MODEL` | override primary model id |
| `SENTINEL_GEMINI_MODEL` | override Gemini model id |
| `SENTINEL_LLM_PROVIDER` | force `openrouter` or `gemini` (else auto order + fallback) |
| `SENTINEL_BASELINE_PACING_S` | seconds between baseline generations in paced runs |

## Repository layout

```
sentinel/            ingest · spec · synth · search · runner · worker · minimize ·
                     report · mutants · baseline · bench · llm · orchestrator · cli
samples/             targets with deliberate seeded defects + correct references
tests/               unit + e2e suite (66 tests) incl. known-bug zoo & FP guards
results*/            benchmark tables + raw per-mutant JSON evidence
DESIGN.md            design & phased plan (implemented; see SPRINT_REPORT.md history)
```

## Development

```bash
pip install -e .[dev]
python -m pytest tests/ -q --cov=sentinel     # 66 tests
ruff check sentinel tests --select F,E9       # errors + pyflakes; style rules intentionally narrow
```

CI runs lint, the test suite with coverage, and a sandboxed end-to-end smoke scan.
LLM keys are not needed for CI; LLM-dependent tests degrade gracefully without them.

## Known limitations

- Python 3.11 single-function targets only; async/generators/I-O-heavy targets skipped.
- The sandbox is not a security boundary (see warning above).
- Baseline generation depends on free-tier provider quotas; artifacts record
  `rate_limited` honestly rather than dropping samples silently.
- QuixBugs-style third-party benchmarks deferred (licensing).

## License

MIT — see [LICENSE](LICENSE).
