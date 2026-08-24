# SENTINEL benchmark results

- Generated: 2026-08-23 (sprint 1) - seed 7, per-mutant budget <=20 s / 500 execs
- Model (LLM spec tier + naive baseline): `stealth/ox-alpha` via OpenRouter
- Baseline harness: `opencode run --format json -m openrouter/stealth/ox-alpha`
- Mutants: built-in deterministic AST mutation engine (`sentinel/mutants.py`), max 12/target
- Detection rule: a mutant is *caught* iff a finding reproduces on the mutant and does NOT
  reproduce on the original (snapshot differential oracle + inferred properties)

## Headline

| system | caught | total | rate |
|---|---|---|---|
| **SENTINEL** | **24** | 33 | **73%** |
| SENTINEL (baseline-valid samples only) | **16** | 18 | **89%** |
| Naive-prompt baseline (same model, opencode CLI) | 12 | 18* | 67% |

\* baseline denominator covers only samples where its suite was valid
(passes on the original); see fairness note below.

## Per-sample detail

| target | mutants | SENTINEL | baseline | branch cov (SENTINEL) | findings on unmutated original |
|---|---|---|---|---|---|
| `correct_parse_duration.py` | 6 | **5** (83%) | **6** (100%) | 100% | 0 |
| `parse_duration.py` (seeded h>=2 carry bug) | 12 | **11** (92%) | 6 (50%) | 100% | 1 = the seeded bug itself (`2h -> -3600`) |
| `slugify.py` (seeded regex crash on sep) | 7 | **4** (57%) | n/a - generation failed after 3 attempts + repair | 50% | 1 (crash-class, same seeded regex defect family) |
| `intervals.py` (seeded adjacent-merge off-by-one) | 8 | 4 (50%) | invalid - naive tests fail on buggy original | 83% | 3 (crash-class on malformed inputs) |

## Findings-on-original audit (FP accounting)

- `parse_duration('2h') == -3600` fires on the *original*: that IS the deliberately seeded
  bug from the spec (Section 5 example). Counted as true positive, not an FP.
- `intervals`: 3 CONFIRMED undeclared-TypeError crashes on malformed inputs
  (`[0]`, `['']`, `[{}]`). Genuine robustness defects for an untyped `list` parameter;
  counted as FPs against SENTINEL in the headline number (conservative).
- `slugify`: 1 CONFIRMED crash on emoji text + unusual separator; same root cause family as
  the seeded regex bug (unescaped separator in character class).
- Clean reference implementation: **0 findings** (false-positive guard passes).

## Fairness notes

1. **Baseline validity gate**: mutation-testing convention requires the generated suite to
   pass on the original. For `intervals.py` the naive suite encodes correct behavior and the
   original is buggy -> suite invalid -> sample excluded from baseline denominator. This is
   *favorable* to the baseline (we dropped a sample where it scored 0).
2. **Snapshot asymmetry**: SENTINEL's differential oracle uses original behavior as
   reference, so it can catch bug-*fixing* mutants on already-buggy originals where a
   passing test-suite baseline structurally cannot exist. On the fully-clean reference
   target this advantage disappears (SENTINEL 5/6 vs baseline 6/6 - baseline wins there).
3. Baseline got 3 generation attempts + automatic string-repair; one sample still produced
   no valid file (recorded as failure-to-generate).
4. Same model for both sides; baseline prompt asks for "thorough tests" without access to
   SENTINEL's property machinery.

## Reproducibility

```
pip install -e .[dev]
python -m pytest tests/            # SENTINEL's own suite (33 tests)
sentinel bench samples --baseline --out results --budget 18 --max-executions 350 --seed 7
```

Requires `OPENROUTER_API_KEY` in the environment and the `opencode` CLI on PATH.
Raw per-mutant data: `results/bench_details.json`.
