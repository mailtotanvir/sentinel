# SENTINEL — An Agent That Proves Your Code Wrong

> **Project spec + context doc for opencode (ox-alpha).**
> Paste into opencode as project context. Then run the kickoff prompt in Section 9.

---

## 1. One-Line Pitch

SENTINEL is a verification agent that takes any function or module and generates a **high-coverage, adversarial test suite** — property-based tests, edge-case inputs, and lightweight inferred specs — then **hunts for bugs** by searching for inputs that break invariants. Automated red-teaming for *correctness*.

The bet: code *generation* is saturated; code *verification* is where the value (and the open problems) are.

## 2. Why This Is Worth Building

- Immediately **useful** — people would actually run this on real code.
- Plays to a strong model's strength: **reasoning about edge cases and invariants**.
- **Clean, honest eval**: % of seeded/mutated bugs caught vs a naive baseline.

## 3. Core Concepts

- **Target**: a function/module under test (the "SUT").
- **Spec Inference**: infer intended behavior (pre/postconditions, invariants) from code + docstrings + names.
- **Property Test**: an invariant that should hold for all valid inputs.
- **Adversarial Search**: coverage- and invariant-guided search for failing inputs.
- **Bug Report**: minimal reproducing input + explanation + suggested fix.

## 4. Architecture

```
+------------------+     +----------------------+
|  Ingestor        |---->|  Spec Inferrer       |
| (parse SUT/AST)  |     | (pre/post/invariant) |
+------------------+     +----------+-----------+
                                    |
                                    v
+------------------+     +----------------------+
|  Test Synthesizer|<--->|  Property Generator  |
| (example + PBT)  |     |  (hypothesis-style)  |
+--------+---------+     +----------+-----------+
         |                          |
         v                          v
+------------------+     +----------------------+
|  Coverage-Guided |---->|   Failure Minimizer  |
|  Input Search    |     |   + Bug Reporter     |
+------------------+     +----------------------+
```

### Components
1. **Ingestor** — parse target via AST; extract signatures, types, docstrings, dependencies.
2. **Spec Inferrer** — infer intended contract: preconditions, postconditions, invariants, error behavior.
3. **Test Synthesizer** — generate example-based tests + property-based tests (hypothesis-style strategies from types).
4. **Coverage-Guided Input Search** — mutate/search inputs to maximize branch coverage and trip invariants.
5. **Failure Minimizer** — shrink failing inputs to a minimal reproducer.
6. **Bug Reporter** — produce a readable report: repro, expected vs actual, root-cause guess, patch suggestion.

## 5. Output Format

```json
{
  "target": "utils.parse_duration",
  "coverage": { "branch": 0.94, "line": 0.98 },
  "properties_checked": ["idempotent_on_normalized", "never_negative"],
  "bugs": [
    {
      "id": "BUG-003",
      "minimal_input": "\"1h60m\"",
      "invariant_violated": "never_negative",
      "expected": ">= 0 seconds",
      "actual": "-3600",
      "root_cause_guess": "carry not applied when minutes >= 60",
      "suggested_patch": "normalize minutes into hours before summing"
    }
  ]
}
```

## 6. Tech Stack

- **Language**: Python 3.11 first (easiest AST + `hypothesis`); design so a JS/TS backend can be added later.
- **Property testing**: `hypothesis` (with a manual fallback generator if unavailable).
- **Coverage**: `coverage.py` for branch/line feedback to guide search.
- **Isolation**: subprocess with timeouts (target code may hang/crash).

## 7. Build Plan (5 Days)

### Day 1 — Ingestor + Runner
- AST parsing: extract signatures, type hints, docstrings.
- Isolated runner: call a target with inputs, capture return/exception/timeout.
- CLI: `sentinel scan <file.py::func>`.
- **Done when:** you can point at a function and execute it on given inputs safely.

### Day 2 — Spec Inference + Basic Tests
- Infer pre/postconditions + invariants from code, names, docstrings.
- Generate first example-based tests targeting obvious edges (empty, zero, nega None, unicode).
- **Done when:** SENTINEL emits a runnable test file for a target.

### Day 3 — Property-Based Tests + Coverage Loop
- Build hypothesis strategies from type hints; encode inferred invariants as properties.
- Wire `coverage.py`; iterate input generation to raise branch coverage.
- **Done when:** coverage climbs across iterations and properties execute.

### Day 4 — Adversarial Search + Minimizer
- Coverage- and invariant-guided search for failing inputs; shrink to minimal repro.
- Bug Reporter with root-cause guess + suggested patch.
- **Done when:** SENTINEL finds and minimizes a real bug on sample code.

### Day 5 — Benchmark vs Baseline
- Use a seeded-bug set (e.g., **QuixBugs** and/or **mutation testing** via `mutmut`/manual mutants).
- Baseline = "write me some tests" naive prompt.
- Measure **bug-catch rate** + coverage; write `RESULTS.md` with the comparison table.
- **Done when:** SENTINEL clearly beats the naive baseline on bugs caught.

## 8. Success Metrics

- **Primary:** % of injected/mutated bugs caught vs naive baseline.
- **Branch coverage** achieved per target.
- **Minimality:** size of reduced reproducers.
- **False-positive rate:** flagged "bugs" that are actually correct behavior (keep low).

## 9. Kickoff Prompt for opencode (paste after this doc)

```
You are building SENTINEL per the attached spec. Start with Day 1.

1. Scaffold: sentinel/ingest/, sentinel/spec/, sentinel/synth/, sentinel/search/,
   sentinel/report/, samples/, results/
2. Implement the Ingestor (AST extraction of functions, signatures, type hints,
   docstrings) and an isolated Runner (execute a target on inputs; capture
   return/exception/timeout in a subprocess).
3. Add CLI: `sentinel scan <file.py::func>` that prints extracted signature +
   runs the function on a couple of hand-given inputs.
4. Add 2-3 sample functions in samples/ (one with a deliberate edge-case bug).
Run the CLI on the samples and show me the output before moving to Day 2.
Provide a manual generator fallback if `hypothesis` is unavailable.
```

## 10. Stretch Goals

- **Metamorphic testing** (relations between related inputs).
- **Differential testing** against a reference implementation.
- **Auto-fix loop**: propose a patch, re-run the suite, confirm the bug is gone.
- **Multi-language** backend (JS/TS via AST tooling).

## 11. Risks & Mitigations

- *False positives from wrong inferred specs* -> confidence-rank specs; separate "confirmed crash" from "possible spec violation."
- *Target code hangs/crashes the runner* -> subprocess isolation + timeouts.
- *Over-fitting to the benchmark* -> hold out a hidden mutant set for final eval.
