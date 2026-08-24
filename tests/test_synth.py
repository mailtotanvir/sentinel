import ast as pyast

from sentinel.fallback_gen import draw, mutate_value, sample_args
from sentinel.ingest import build_target_ir, parse_selection
from sentinel.spec import infer_spec
from sentinel.synth import seed_inputs, strategy_expr_for, synthesize_all

import random


def test_seed_inputs_cover_edges():
    ir = build_target_ir(parse_selection("samples/parse_duration.py::parse_duration"))[0]
    seeds = seed_inputs(ir)
    flat = {repr(s[0]) for s in seeds if s}
    assert repr("") in flat and repr("2h") in flat and repr("1h60m") in flat


def test_draw_deterministic_with_seed():
    r1 = [draw("int", random.Random(1)) for _ in range(10)]
    r2 = [draw("int", random.Random(1)) for _ in range(10)]
    assert r1 == r2


def test_mutate_reaches_digit_and_punct_classes():
    rng = random.Random(9)
    outs = {repr(mutate_value("1h60m", rng)) for _ in range(400)}
    assert any(any(c.isdigit() for c in o) for o in outs), "no digit mutations"
    assert any(any(c in "!@#$%^&*()[]{}|\\:;" for c in o) for o in outs), "no punct mutations"


def test_sample_args_respects_params():
    ir = build_target_ir(parse_selection("samples/slugify.py::slugify"))[0]
    args = sample_args(ir.params, random.Random(3))
    assert isinstance(args, list)


def test_strategy_exprs_compile():
    for ann in ["int", "str", "float", "list[int]", "Optional[str]", "Tuple[int, int]",
                "Dict[str, int]", None]:
        expr = strategy_expr_for(ann)
        pyast.parse(expr)  # valid python expression syntax


def test_emitted_tests_are_valid_python_and_runnable(tmp_path):
    ir = build_target_ir(parse_selection("samples/correct_parse_duration.py::parse_duration"))[0]
    props = infer_spec(ir)
    paths = synthesize_all(ir, props, str(tmp_path))
    import subprocess, sys
    for label, path in paths.items():
        if path is None:
            continue
        compile(open(path).read(), path, "exec")
        res = subprocess.run([sys.executable, "-m", "pytest", "-q", path],
                             capture_output=True, text=True, timeout=300)
        assert res.returncode == 0, f"{label} emitted tests failed:\n{res.stdout[-800:]}"
