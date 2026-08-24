"""Sprint 2: cross-param search power, mutant engine v2, multi-seed bench stats."""
import ast

import pytest

from sentinel import fallback_gen, mutants as mut_engine, synth


def _ir(spec):
    from sentinel.ingest import build_target_ir, parse_selection
    return build_target_ir(parse_selection(spec))[0]


# -- S2.1 coordinated combo seeds ---------------------------------------------------

def test_combo_seeds_embed_separator_in_text():
    ir = _ir("samples/slugify.py::slugify")
    seeds = synth.seed_inputs(ir, cap=40)
    # at least one seed must place a multi-char separator next to whitespace,
    # the only shape that reaches the collapse loop
    assert any(len(s) >= 2 and isinstance(s[0], str) and isinstance(s[1], str)
               and len(s[1]) > 1 and s[1] in s[0] and " " in s[0]
               for s in seeds), seeds[-8:]


def test_combo_seeds_reach_collapse_branch():
    ir = _ir("samples/slugify.py::slugify")
    seeds = synth.seed_inputs(ir, cap=40)
    hit = sum(1 for s in seeds
              if len(s) >= 2 and s[0] == "a-- b" and s[1] == "--")
    assert hit, "coordinated family ('a-- b', '--') missing from seeds"


def test_single_param_targets_get_no_combo_seeds():
    ir = _ir("samples/parse_duration.py::parse_duration")
    seeds = synth.seed_inputs(ir, cap=40)
    assert all(not isinstance(s[0], tuple) for s in seeds if s)


# -- S2.2 joint mutation probe --------------------------------------------------------

def test_joint_probe_coordinates_two_str_params():
    import random
    rng = random.Random(11)
    out = fallback_gen.joint_collapse_probe(["x", "-", 5], rng)
    assert out is not None
    assert isinstance(out[0], str) and out[1] in fallback_gen.JOINT_SEPS
    assert out[2] == 5  # untouched params preserved


def test_joint_probe_none_without_two_strings():
    import random
    assert fallback_gen.joint_collapse_probe(["x", 3], random.Random(0)) is None


def test_mutate_args_applies_joint_probe_sometimes():
    import random
    from sentinel.search import mutate_args
    ir = _ir("samples/slugify.py::slugify")
    rng = random.Random(42)
    joint = 0
    for _ in range(200):
        out = mutate_args(["abc", "-"], ir.params, rng)
        if isinstance(out[0], str) and isinstance(out[1], str) \
                and out[1] in fallback_gen.JOINT_SEPS:
            joint += 1
    assert joint > 5, "joint probe effectively never fires"


# -- S2.3 mutant engine v2 --------------------------------------------------------------

def test_new_mutant_classes_generated():
    all_kinds = set()
    for f in ("samples/parse_duration.py", "samples/intervals.py",
              "samples/slugify.py"):
        src = open(f).read()
        for d, s in mut_engine.unique_mutants(src):
            all_kinds.add(d.split("@")[0].split(":")[0])
            ast.parse(s)  # every mutant must be valid python
    assert {"Del", "Slice", "AugAssign"} <= all_kinds, all_kinds


def test_deletion_mutant_removes_statement():
    src = open("samples/intervals.py").read()
    dels = [(d, s) for d, s in mut_engine.unique_mutants(src)
            if d.startswith("Del:")]
    assert dels
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef))
    n_base = len(fn.body)
    for _, s in dels[:3]:
        # deletion sites are direct function-body statements: exactly one removed
        m_fn = next(n for n in ast.parse(s).body if isinstance(n, ast.FunctionDef))
        assert len(m_fn.body) == n_base - 1


def test_slice_mutant_shifts_bound_by_one():
    src = open("samples/intervals.py").read()
    sl = [(d, s) for d, s in mut_engine.unique_mutants(src)
          if d.startswith("Slice@")]
    assert sl, "expected slice off-by-one mutants on intervals.py"
    assert "ivs[1:]" not in sl[0][1] or "ivs[0:]" in sl[0][1]


# -- S2.4 multi-seed bench stats ---------------------------------------------------------

def test_totals_multiseed_stats():
    from sentinel.bench import _totals
    results = {
        "a::f": {"mutants": 10, "sentinel_caught": 17,
                 "per_seed": [{"seed": 7, "caught": 6}, {"seed": 1007, "caught": 5},
                              {"seed": 2007, "caught": 6}],
                 "original_findings_fp": 1, "original_findings": [{"severity": "SUSPECTED"}]},
        "b::g": {"mutants": 10, "sentinel_caught": 16,
                 "per_seed": [{"seed": 7, "caught": 9}, {"seed": 1007, "caught": 7},
                              {"seed": 2007, "caught": 8}],
                 "original_findings_fp": 0, "original_findings": []},
    }
    tot = _totals(results)
    rates = [15 / 20, 12 / 20, 14 / 20]  # per-seed totals across both entries
    assert tot["seed_count"] == 3
    assert tot["sentinel_rate_min"] == min(rates)
    assert tot["sentinel_rate_max"] == max(rates)
    assert tot["sentinel_rate_mean"] == pytest.approx(sum(rates) / 3)
    assert tot["fp_confirmed"] == 0 and tot["fp_suspected"] == 1


@pytest.fixture()
def _args(tmp_path):
    import argparse
    return argparse.Namespace(seed=7, seeds=3, holdout_seed=None, out=str(tmp_path),
                              baseline=False, baseline_rest=False,
                              budget=18, max_executions=350)


def test_results_md_contains_per_seed_section(_args):
    from sentinel.bench import _totals, _write_results_md
    results = {
        "a::f": {"mutants": 4, "sentinel_caught": 3, "coverage_branch": 1.0,
                 "per_seed": [{"seed": 7, "caught": 3}, {"seed": 1007, "caught": 2}],
                 "original_findings_fp": 0, "original_findings": []},
    }
    tot = _totals(results)
    _write_results_md(_args, results, tot)
    md = open(f"{_args.out}/RESULTS.md").read()
    assert "Per-seed reproducibility" in md
    assert "mean 62%" in md  # f"{0.625:.0%}" formatting
    assert "seeds: 3" in md  # header reflects args.seeds
