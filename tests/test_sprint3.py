"""Sprint 3: structural pair mutators, portable PBT emission, --all summary."""
import os
import random
import runpy

from sentinel import fallback_gen, synth


def _ir(spec):
    from sentinel.ingest import build_target_ir, parse_selection
    return build_target_ir(parse_selection(spec))[0]


# -- S3.1 gap seeds + structure-preserving mutation -----------------------------------

def test_mined_targets_also_get_pair_prior():
    # intervals.py mines [1,2],[2,3] from its docstring AND must still receive
    # the gap-separated prior shapes (regression: mined path skipped the prior)
    ir = _ir("samples/intervals.py::merge_intervals")
    seeds = synth.seed_inputs(ir, cap=40)
    gaps = [s for s in seeds if s and isinstance(s[0], list) and len(s[0]) >= 2
            and isinstance(s[0][0], (list, tuple)) and s[0][-1][0] > s[0][0][1]]
    assert gaps, "gap-shaped pair seeds missing on a docstring-mined target"


def test_pair_shaped_lists_get_structure_preserving_ops():
    rng = random.Random(9)
    v = [(0, 1), (5, 6)]
    preserved = 0
    for _ in range(300):
        m = fallback_gen.mutate_value(v, rng)
        if isinstance(m, list) and m and fallback_gen._pair_shaped(m) and \
                all(isinstance(e, tuple) for e in m):
            preserved += 1
    assert preserved > 30, "pairshift/gapify ops never fire"


def test_gapify_forces_separation():
    rng = random.Random(3)
    forced = 0
    for _ in range(200):
        m = fallback_gen._mutate([(0, 1), (2, 3)], rng, "tweak")
        if isinstance(m, list) and len(m) == 2 and fallback_gen._pair_shaped(m):
            if any(e[0] > m[i - 1][1] for i, e in enumerate(m) if i > 0):
                forced += 1
    assert forced > 10, "gapify op never produced separated intervals"


def test_non_pair_lists_still_type_confused():
    rng = random.Random(9)
    flips = 0
    for _ in range(100):
        m = fallback_gen.mutate_value([("a", 1), ("b", 2)], rng)
        if not fallback_gen._pair_shaped(m):
            flips += 1
    assert flips > 20


def test_scan_all_writes_summary(tmp_path):
    import subprocess
    import sys
    out = str(tmp_path / "s")
    res = subprocess.run(
        [sys.executable, "-m", "sentinel.cli", "scan",
         "samples/correct_intervals_ref.py", "--all", "--out", out,
         "--budget", "8", "--max-executions", "150", "--seed", "3"],
        capture_output=True, text=True, timeout=280,
        env=dict(os.environ))
    summary_md = os.path.join(out, "summary.md")
    assert os.path.exists(summary_md), res.stdout[-500:]
    md = open(summary_md).read()
    assert "`merge_intervals`" in md
    summary_json = os.path.join(out, "summary.json")
    data = __import__("json").load(open(summary_json))
    assert data["targets"][0]["coverage"]["branch"] > 0


# -- S3.4 portable PBT emission ---------------------------------------------------------

def test_emitted_pbt_has_no_absolute_module_path(tmp_path):
    ir = _ir("samples/parse_duration.py::parse_duration")
    p = synth.emit_pbt_tests(ir, [], str(tmp_path))
    src = open(p).read()
    line = next(l for l in src.splitlines() if l.startswith("MODULE_PATH"))
    # must be _HERE-joined relative resolution, never a hardcoded absolute path
    assert "os.path.join(_HERE" in line
    assert "', '/" not in line and '", "/' not in line


def test_emitted_pbt_resolves_and_runs_green(tmp_path):
    from sentinel.spec import infer_spec
    ir = _ir("samples/correct_parse_duration.py::parse_duration")
    p = synth.emit_pbt_tests(ir, infer_spec(ir), str(tmp_path))
    ns = runpy.run_path(p)  # __file__ set -> relative MODULE_PATH resolves
    ns["test_properties_fallback"]()  # must pass on the correct implementation
