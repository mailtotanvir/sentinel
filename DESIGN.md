# SENTINEL — Design & Implementation Plan

> Companion to `SENTINEL_verifier_agent.md` (the spec). This document turns the spec into a
> concrete, reviewable design: component contracts, data models, process/isolation model,
> search algorithm, eval methodology, and a phased implementation plan with acceptance criteria.
>
> Status: **IMPLEMENTED — v0.1.0** (all phases accepted; build history in
> `SPRINT_REPORT.md`, open questions resolved in Section 12).

---

## 1. Scope and Non-Goals

**In scope (v1):**
- Python 3.11 targets only; single public functions or methods resolvable as `file.py::qualname`.
- Automated pipeline: ingest -> infer spec -> synthesize tests -> adversarial search ->
  minimize -> report.
- Deterministic, offline operation. LLM assistance is a **pluggable, optional** enhancer
  (see Section 3.3); the system must be fully functional without it.
- Benchmarking against seeded/mutated bugs with a naive-prompt baseline.

**Non-goals (v1):**
- JS/TS or other languages (design keeps language-specific code behind an interface, Section 6).
- Concurrency/async targets, C extensions, I/O-heavy functions (network/disk) — flagged and skipped.
- Proving correctness. SENTINEL searches for *counterexamples*; absence of failure is not proof.

---

## 2. Architecture Overview

```
                    sentinel scan <file.py::func> [--out report.json]
                                      |
                                      v
 +----------------------------------------------------------------------------+
 |                                Orchestrator                                |
 |  (pipeline state machine, budgets, cancellation, artifact persistence)     |
 +----+---------------+----------------+----------------+---------------+-----+
      |               |                |                |               |
      v               v                v                v               v
 +----------+   +-----------+    +------------+   +-----------+   +-----------+
 | Ingestor |-->| Spec      |    | Test       |   | Coverage- |   | Failure   |
 | AST ->   |   | Inferrer  |--->| Synthesizer|-->| Guided    |-->| Minimizer |
 | TargetIR |   | heuristics|    | examples+  |   | Input     |   | ddmin /   |
 |          |   | (+LLM opt)|    | PBT        |   | Search    |   | shrinker  |
 +----------+   +-----------+    +------------+   +-----+-----+   +-----+-----+
                                                       |               |
                                                       v               v
                                                 +-----------+   +-----------+
                                                 | Runner    |<--| Bug       |
                                                 | subprocess|   | Reporter  |
                                                 | sandbox   |   | JSON+MD   |
                                                 +-----------+   +-----------+
```

**Dataflow:** every stage consumes/produces typed records defined in Section 4. The
Orchestrator owns the only mutable pipeline state (a `ScanSession`); stages are pure-ish
functions over these records so they can be unit-tested in isolation and replayed from disk.

---

## 3. Component Design

### 3.1 Ingestor — `sentinel/ingest.py`

Parses the target file with Python's `ast` module and produces a **`TargetIR`**:

```python
@dataclass(frozen=True)
class TargetIR:
    module_path: str          # absolute path to source file
    qualname: str             # e.g. "utils.parse_duration"
    kind: Literal["function", "method"]     # async rejected in v1
    signature: str            # rendered "(s: str, default: int = 0) -> int"
    params: list[Param]       # name, annotation (resolved string), default, kind
    return_annotation: str | None
    docstring: str | None     # raw
    source_segment: str       # function source (for LLM context)
    module_functions: list[str]  # sibling names (for round-trip pairing)
    decorators: list[str]     # non-empty => "may have side effects" caveat
```

Rules:
- Selection supports `file.py::func`, `file.py::Class.method`, and bare `file.py` (scan all
  top-level functions).
- Missing/unresolvable annotations are recorded, not fatal; downstream strategy building
  degrades to a conservative `AnyStrategy` (Section 3.4).
- Async targets, generator-only targets, and targets decorated with known side-effect
  markers (`app.route`, `task`, ...) are skipped with an explicit reason in the report.

### 3.2 Runner — `sentinel/runner.py` + `sentinel/worker.py`

All target execution happens in **worker subprocesses**; the orchestrator never executes
target code in-process.

- **Worker protocol:** parent spawns `python -m sentinel.worker`; sends one JSON request per
  line on stdin (`{"id": ..., "call": {"args": [...], "kwargs": {...}}}`), reads one JSON
  response per line on stdout. One worker handles a batch of calls until killed, amortizing
  interpreter startup during search.
- **Timeouts:** parent enforces a wall-clock deadline per call (default 2 s) plus a total
  budget per phase. On timeout it kills the worker's process group
  (`start_new_session=True` + `os.killpg`). A hung call returns status `TIMEOUT` and counts
  as a finding for non-generator functions (hangs are bugs).
- **Resource caps:** worker sets `resource.setrlimit` for CPU time, address space (OOM
  guard), and NPROC where supported. Environment scrubbed except `PYTHONPATH` pointing at a
  temp sandbox that imports the target module.
- **Result record:** `{status: OK | EXCEPTION | TIMEOUT | OOM | WORKER_CRASH, value,
  exc_type, exc_repr, traceback_tail}`.
- **Determinism guard:** candidate failures are re-executed twice; only failures reproducing
  both times escalate to findings (cuts flaky false positives, spec §8).

### 3.3 Spec Inferrer — `sentinel/spec.py`

Produces a **`SpecContract`**: a ranked set of candidate properties, each with provenance
and confidence. Three tiers:

1. **Heuristic tier (always on, deterministic):**
   - *Docstring mining:* parse Google/NumPy/reST styles. `Raises:` sections become exception
     contracts; `Returns:` phrases ("returns non-negative") map to bound predicates;
     param notes ("must be sorted") become preconditions.
   - *Name-based priors:* `is_/has_` prefix implies bool-return contract; `parse_/from_`
     implies a round-trip pair (`to_/serialize/dump`) discovered among sibling functions;
     mentions of `empty/zero/min/max/limit` mark boundary candidates.
   - *Exception audit:* static scan of `raise` statements yields the declared exception set.
     Any exception type outside it that escapes at runtime is a **crash-class finding**
     (highest confidence, no spec ambiguity).
2. **Property template tier (always on):** generic invariants valid for broad classes of
   pure functions.

   | Template        | Applicability check              | Property                        |
   |-----------------|----------------------------------|---------------------------------|
   | never_crash     | all                              | no undeclared exception         |
   | deterministic   | no obvious I/O / global mutation | f(x) == f(x) across calls       |
   | idempotent      | normalization-shaped name/type   | f(f(x)) == f(x)                 |
   | round_trip      | paired inverse fn found          | inv(f(x)) == x                  |
   | exception_contract | Raises section present        | only declared exceptions escape |
   | range_bound     | docstring/naming evidence        | output satisfies mined predicate |

3. **LLM tier (optional, gated behind `--llm` or `SENTINEL_LLM=1`):** send
   `source_segment + docstring + callee signatures` to the configured model; request JSON
   pre/postconditions and invariants with confidence scores. All LLM output is treated as
   **untrusted suggestions**: predicates must compile in a restricted sandbox before use and
   are labeled `provenance: "llm"` so SUSPECTED findings stay distinct from CONFIRMED ones
   (spec §11 mitigation).

Every property carries `confidence in [0,1]` and `provenance`. Downstream, the Reporter
separates **CONFIRMED** (crash / timeout / undeclared exception — directly observed) from
**SUSPECTED** (violated an inferred predicate — the spec may be wrong).

### 3.4 Test Synthesizer — `sentinel/synth.py`

Two artifacts:

1. **Example tests** written to `tests/sentinel_examples_<name>.py`, runnable standalone:
   edge lattice derived from annotations — empty containers, zero/negative values, None,
   max-int, unicode strings (emoji, RTL, combining marks), whitespace-only, deep-but-bounded
   nesting, float `inf/nan/-0.0`. Each asserts expected behavior when spec confidence is
   high, else only "no undeclared exception".
2. **Property-based tests** using `hypothesis` when installed:
   - Strategy builder maps annotations to strategies recursively: `int -> integers()`
     (bounded by inferred preconditions), `list[T] -> lists(strategy(T), max_size=32)`,
     unions -> `one_of(...)`, unannotated params -> conservative pool of text/integers.
   - Inferred properties become `@given` test bodies; settings: `max_examples=200`,
     `deadline=None` (timeout owned by Runner), health checks left on.
   - **Manual fallback generator** (spec §9 requirement): if `hypothesis` is unavailable, a
     seeded-RNG shim implements the same `draw(annotation)` API so search still runs;
     shrinking then falls back to ddmin (Section 3.5).

### 3.5 Coverage-Guided Input Search — `sentinel/search.py`

The core loop: maximize branch coverage while hunting invariant violations.

```
corpus = seeds(synthesized examples + boundary probes)
loop until budget exhausted:
    input = mutate(sample_from_corpus())           # energy proportional to novelty
    cov_arcs, result = run_with_coverage(input)    # branch data collected in worker
    if result violates a property:
        queue_for_minimization(input, violated_property)
    if cov_arcs contains arcs not in corpus union:
        corpus.add(input)                          # coverage-novelty bonus
    periodically prune inputs fully subsumed by newer corpus entries
```

Design points:
- **Coverage measurement:** worker wraps each batch in `coverage.Coverage(branch=True)` with
  `include=[target_file]`; per-call arc sets diffed against the corpus union. Overhead is
  acceptable because we measure only the target module.
- **Mutation operators:** numeric +-1 / +-epsilon / boundary flip; string splice, char swap,
  injection of format tokens (`%s`, `{}`, NUL byte, `"0"` string-vs-int confusion); container
  grow/shuffle/inject None or wrong-typed element (type-confusion probes run guarded inside
  the worker).
- **Budgets:** default `--budget 60s` wall clock and `--max-executions 2000` per phase;
  both CLI-overridable. Search stops early when coverage plateaus for K iterations AND the
  failure queue is empty.
- **Energy schedule:** simple fitness = number of novel arcs + recency decay; no need for a
  full AFL-style scheduler in v1.

### 3.6 Failure Minimizer — `sentinel/minimize.py`

- **Primary:** hypothesis's built-in shrinking (free when PBT found the failure).
- **Secondary** (search-found or fallback mode): greedy delta-debugging (ddmin) over the
  structured input representation — shrink lists toward empty, strings toward shorter
  prefixes, numbers toward boundary values (`0`, `1`, `-1`); re-check the invariant is still
  violated after every candidate reduction.
- Output: `minimal_input` plus diff vs original finding. Minimality metric (spec §8) =
  serialized minimal input length / original input length.

### 3.7 Bug Reporter — `sentinel/report.py`

Emits two artifacts per scan:

1. **`report.json`** conforming to spec §5 schema, extended:

```json
{
  "target": "utils.parse_duration",
  "coverage": {"branch": 0.94, "line": 0.98},
  "properties_checked": [
    {"name": "never_negative", "provenance": "docstring", "confidence": 0.9}
  ],
  "bugs": [
    {
      "id": "BUG-003",
      "severity_class": "CONFIRMED",
      "minimal_input": "\"1h60m\"",
      "invariant_violated": "never_negative",
      "expected": ">= 0 seconds",
      "actual": "-3600",
      "root_cause_guess": "carry not applied when minutes >= 60",
      "suggested_patch": "normalize minutes into hours before summing"
    }
  ],
  "meta": {
    "sentinel_version": "0.1.0",
    "budget_used_s": 58,
    "executions": 1841,
    "skipped_reasons": []
  }
}
```

2. **Human-readable `report.md`:** findings grouped by severity class, repro snippets,
   generated test file paths.

Root-cause guessing in v1 is static-only: map the failing execution's coverage arcs to the
nearest conditional and describe the violated property in natural language. `--llm`
optionally upgrades this to a model-generated explanation.

---

## 4. Core Data Models — `sentinel/models.py`

Shared JSON-serializable dataclasses used across all stages:

| Record | Produced by | Consumed by |
|---|---|---|
| `TargetIR` | Ingestor | Spec Inferrer, Synthesizer, Runner sandbox |
| `SpecContract` (list of `Property`) | Spec Inferrer | Synthesizer, Search, Reporter |
| `TestCaseFile` (path + kind) | Synthesizer | Orchestrator (persisted), user |
| `ExecResult` | Runner | Search, Minimizer, Reporter |
| `CoverageDelta` | Runner / Search | Search corpus manager |
| `Finding` (bug candidate) | Search | Minimizer -> Reporter |
| `ScanReport` | Reporter | CLI stdout / `--out` file |

Every record round-trips through JSON so a session can be replayed offline
(`sentinel replay session.json`) — important for debugging SENTINEL itself.

## 5. CLI Surface

```
sentinel scan <file.py::func> [--out DIR] [--budget SECONDS] [--max-executions N]
              [--llm] [--seed N] [--json]
sentinel bench <samples_dir> [--baseline] [--out results/]
sentinel replay <session.json>
```

Exit codes: `0` no bugs found, `1` bugs found, `2` usage/target error. `--json` prints the
report to stdout for CI use.

## 6. Repository Layout

```
sentinel/
  pyproject.toml            # package; deps: coverage; extras: hypothesis
  sentinel/
    __init__.py
    cli.py                  # argparse entrypoint
    models.py               # shared dataclasses (Section 4)
    ingest.py
    worker.py               # subprocess entry: exec + coverage collection
    runner.py               # process management, budgets, IPC
    spec.py                 # heuristic + template tiers; llm plugin point
    synth.py                # example tests, strategy builder, fallback generator
    search.py               # corpus, mutations, energy schedule
    minimize.py             # ddmin + hypothesis-shrink wrapper
    report.py               # JSON + Markdown emitters
    bench.py                # mutant harness, baseline runner, scoring
  samples/
    parse_duration.py       # deliberate carry bug (spec example)
    slugify.py              # unicode edge cases
    intervals.py            # overlap logic with off-by-one
  tests/                    # SENTINEL's own unit tests
  results/                  # bench outputs, RESULTS.md
```

Language extensibility (JS/TS later): stages depend only on `TargetIR`, `ExecResult`, and a
`LangBackend` protocol (`parse_target`, `build_strategy`, `make_worker`). Python is the sole
v1 backend.

## 7. Implementation Plan (phased; maps to spec §7 five days)

Each phase has explicit acceptance criteria; do not advance until they pass.

### Phase 0 — Scaffold (half day)
- Repo layout per Section 6; `pyproject.toml`; pytest wired; ruff config.
- Housekeeping: remove stray `SENTINEL_verifier_agent.md:Zone.Identifier` artifact.
- **Done when:** `pip install -e . && pytest` green on empty suite; `sentinel --help` works.

### Phase 1 — Ingestor + Runner (= spec Day 1)
- `models.py`: `TargetIR`, `Param`, `ExecResult`.
- `ingest.py`: AST walk; signature extraction via AST + docstring/source capture; target
  selection syntax (`file.py::func`, `file.py::Class.method`, bare file).
- `worker.py` + `runner.py`: line-delimited JSON IPC, per-call timeout, process-group kill,
  rlimits, determinism double-run helper.
- CLI: `sentinel scan samples/parse_duration.py::parse_duration --probe '60'`.
- Samples: 3 files from Section 6 (`parse_duration` carries the deliberate negative-carry
  bug).
- **Acceptance:** each sample function executes safely on given inputs; a forced-hang sample
  returns TIMEOUT within deadline with no orphan processes; unit tests cover selection
  parsing, timeout kill, IPC framing.

### Phase 2 — Spec Inference + Example Tests (= spec Day 2)
- `spec.py` heuristic tier: docstring mining (Google/NumPy/reST), name priors, exception
  audit producing the declared-raise set.
- Property templates: never_crash, deterministic, idempotent, exception_contract.
- `synth.py` example-lattice emitter writing runnable pytest files.
- **Acceptance:** for each sample SENTINEL emits a test file that runs under pytest; emitted
  tests catch the seeded `parse_duration` bug when run manually; inferred contracts match
  hand-written expectations (golden-file test).

### Phase 3 — PBT + Coverage Loop (= spec Day 3)
- Hypothesis strategy builder from annotations + fallback generator shim (spec §9).
- Wire `coverage.py` into worker; arc-diff corpus manager; energy schedule; budget control.
- **Acceptance:** branch coverage on samples climbs monotonically to >= 85% during search
  (asserted in integration test); properties execute under both hypothesis and fallback
  paths (test matrix with/without hypothesis installed).

### Phase 4 — Adversarial Search + Minimizer + Reporter (= spec Day 4)
- Mutation operators; failing-input queue; ddmin minimizer; CONFIRMED/SUSPECTED
  classification; `report.json` + `report.md` emitters; static root-cause heuristic.
- **Acceptance:** end-to-end `sentinel scan samples/parse_duration.py::parse_duration`
  finds the negative-result bug, shrinks it to a minimal repro, and writes both artifacts
  passing schema-validation tests; false-positive check: scanning a correct reference
  implementation yields zero CONFIRMED findings.

### Phase 5 — Benchmark vs Baseline (= spec Day 5)
- `bench.py`:
  - Mutants via `mutmut` on samples + curated modules; QuixBugs Python targets where license
    permits (spec §7).
  - Detection rule: mutant is *caught* iff a CONFIRMED finding reproduces on the mutant but
    not on the original; SUSPECTED findings tallied separately.
  - Baseline: one-shot naive prompt ("write thorough tests for this function") executed
    against the same mutants under the same detection rule.
- Metrics (spec §8): bug-catch rate, branch coverage, median minimized-input size,
  false-positive rate (findings on unmutated originals).
- **Done when:** `results/RESULTS.md` contains the comparison table and SENTINEL beats the
  baseline on catch rate at equal-or-lower false positives.

Phases are strictly sequential: each consumes the prior phase's records.

## 8. Eval Methodology Details

- **Mutant hygiene:** exclude import/syntax-breaking mutants from denominators; equivalent
  mutants surviving BOTH systems are reported but not penalized against SENTINEL.
- **Fairness:** baseline receives wall-clock budget = SENTINEL scan budget x3 for test
  authorship + execution; documented in RESULTS.md.
- **Reproducibility:** fixed RNG seed per run (`--seed`); RESULTS.md reports mean + min/max
  over 3 seeds.

## 9. Testing Strategy for SENTINEL Itself

- Unit tests per component against synthetic mini-targets in `tests/fixtures/`.
- Known-bug zoo: fixture functions each carrying exactly one seeded defect class
  (off-by-one, unicode mishandling, missing None guard, overflow, hang) — integration tests
  assert the pipeline catches each one.
- False-positive guard suite: functions that look buggy but are correct (intentional
  ValueError raises) — assert zero CONFIRMED findings.
- Process-hygiene tests: hanging targets leave no surviving children after scan.

## 10. Key Design Decisions & Tradeoffs

| Decision | Rationale | Alternative rejected |
|---|---|---|
| LLM optional, not core | Deterministic, offline, free; honest eval attribution | LLM-in-loop generation (nondeterminism, cost) |
| Batch worker subprocesses, not threads | Targets may hang/crash/clobber globals (spec §11) | In-process execution with faulthandler |
| Line-delimited JSON IPC | Debuggable; no pickle deserialization risk | pickle / RPC frameworks |
| Arc-level coverage fitness | Cheap fine-grained feedback for search | Line-only coverage (coarser); symbolic execution (heavy deps) |
| ddmin on structured values, not text | Type-aware shrinking converges faster | Textual delta debugging on repr strings |
| CONFIRMED vs SUSPECTED split | Controls false-positive rate honestly (spec §8, §11) | Single flat bug list |
| mutmut + QuixBugs benchmark | Standard, reproducible; mutation gives bug density | Hand-written bugs only (thin benchmark) |

## 11. Risks & Mitigations (extends spec §11)

| Risk | Mitigation |
|---|---|
| Wrong inferred specs cause noise | SUSPECTED class + confidence ranking + user override file `.sentinelspec.py` pinning specs per module |
| Coverage overhead slows search | Measure target module only; batch calls per worker; budget knobs |
| Mutants too easy or too hard | Report difficulty mix; hold out a hidden mutant set for final eval (spec §11) |
| Worker IPC desync on target stdout spam | Redirect target stdout/stderr to temp files in worker; never through the protocol pipe |
| Non-deterministic targets flake findings | Double-run determinism guard before escalating to Finding |

## 12. Open Questions for Review — RESOLVED 2026-08-23

1. **LLM tier scope** — **RESOLVED: included.** `--llm` enables the OpenRouter tier;
   model pinned to `stealth/ox-alpha` (auth via `OPENROUTER_API_KEY` already present in
   the environment; no separate key management). All LLM output remains untrusted
   suggestion-tier input (validated + provenance-tagged).
2. **Baseline definition** — **RESOLVED: same model via opencode CLI**:
   `opencode run -m openrouter/stealth/ox-alpha "<naive test-writing prompt>"`.
   One automatic regeneration retry if the emitted file does not parse.
3. **QuixBugs licensing** — v1 benchmark uses the built-in AST mutation engine only
   (`sentinel/mutants.py`, deterministic, seeded); QuixBugs deferred to avoid vendoring
   questions. Spec allows "mutmut / manual mutants"; this is the manual-mutants path.
4. **Default budgets** — scan default 60 s wall clock, 2 s per-call timeout, CLI-overridable
   (`--budget`, `--timeout`, `--max-executions`).
5. **Report location** — `--out DIR` writes `report.json` + `report.md`; when omitted,
   artifacts land in `.sentinel/<target-stem>/`. Session log appended to
   `.sentinel/sessions.jsonl` for `sentinel replay`.

## 13. Stretch Goals (post-v1, from spec §10)

- Metamorphic testing: relations between related inputs (e.g., `f(x + k)` vs `f(x)`).
- Differential testing against a reference implementation supplied by the user.
- Auto-fix loop: propose patch, re-run suite, confirm bug gone (requires LLM tier).
- JS/TS backend via `LangBackend` interface.
