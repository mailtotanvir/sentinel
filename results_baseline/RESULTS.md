# SENTINEL benchmark results

- Generated: 2026-08-24T03:22:27
- Model/LLM tier + baseline model: `stealth/ox-alpha` via OpenRouter (LLM tier falls back to `gemini-3.6-flash`); baseline harness: direct REST temperature=0 (openrouter->gemini fallback; used: gemini)
- Seed: 7; per-mutant budget: <=20s / 500 execs

| target | mutants | SENTINEL caught | rate | baseline caught | baseline rate | branch cov | FP on originals |
|---|---|---|---|---|---|---|---|
| `correct_intervals_ref.py::merge_intervals` | 5 | 4 | 80%-80% (mean 80%) | 4/5 | 80% | 100% | 4 |
| `correct_parse_duration.py::parse_duration` | 8 | 7 | 88%-88% (mean 88%) | 7/8 | 88% | 100% | 0 |
| `intervals.py::merge_intervals` | 5 | 4 | 80%-80% (mean 80%) | invalid_tests_fail_on_original | n/a | 100% | 4 |
| `parse_duration.py::parse_duration` | 13 | 12 | 92%-92% (mean 92%) | invalid_tests_fail_on_original | n/a | 100% | 1 |
| `slugify.py::slugify` | 8 | 5 | 62%-62% (mean 62%) | invalid_tests_fail_on_original | n/a | 100% | 1 |

**TOTAL**: SENTINEL 32/39 (82%); baseline 11/39 (28%); findings on unmutated originals: 10 (CONFIRMED 1 / SUSPECTED 9; SUSPECTED = spec-ambiguous garbage-shape inputs or the target's own seeded defect, see FP audit)

**Per-seed reproducibility** (DESIGN section 8, 3 seeds): mean 82%, min 82%, max 82%

**Holdout eval** (disjoint mutants, seed=99): SENTINEL 35/42 (83%) - overfitting check per spec section 11
