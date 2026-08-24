import textwrap

from sentinel.ingest import build_target_ir, parse_selection
from sentinel.spec import infer_spec, load_user_overrides


def ir_of(path_spec):
    return build_target_ir(parse_selection(path_spec))[0]


def test_mined_bound_and_raises():
    ir = ir_of("samples/parse_duration.py::parse_duration")
    props = infer_spec(ir)
    kinds = {p.kind for p in props}
    assert "range_bound" in kinds and "exception_contract" in kinds
    bound = next(p for p in props if p.kind == "range_bound")
    assert bound.params["expr"] == "result >= 0"
    ec = next(p for p in props if p.kind == "exception_contract")
    assert "ValueError" in ec.params["declared"]


def test_roundtrip_pair_discovery(tmp_path):
    p = tmp_path / "codec.py"
    p.write_text(textwrap.dedent('''
        def encode_obj(x: dict) -> str:
            """Encode dict to string."""
            return str(x)

        def decode_obj(s: str) -> dict:
            """Decode string back."""
            return {}
    '''))
    ir = build_target_ir(parse_selection(str(p) + "::encode_obj"))[0]
    props = infer_spec(ir)
    rt = [p2 for p2 in props if p2.kind == "round_trip"]
    assert rt and rt[0].params["inverse"].startswith("decode")


def test_idempotent_name_prior(tmp_path):
    p = tmp_path / "norm.py"
    p.write_text("def normalize_ws(s: str) -> str:\n    return ' '.join(s.split())\n")
    ir = build_target_ir(parse_selection(str(p) + "::normalize_ws"))[0]
    assert any(pp.kind == "idempotence" for pp in infer_spec(ir))


def test_user_override_file(tmp_path):
    p = tmp_path / "thing.py"
    p.write_text("def f(x: int) -> int:\n    return x\n")
    (tmp_path / ".sentinelspec.py").write_text(
        "PROPERTIES = {'f': [{'name': 'user_nonneg', 'kind': 'range_bound',"
        " 'params': {'expr': 'result >= 0'}, 'confidence': 1.0}]}\n")
    ir = build_target_ir(parse_selection(str(p) + "::f"))[0]
    overrides = load_user_overrides(ir)
    assert overrides and overrides[0].provenance == "user"
    props = infer_spec(ir)
    assert any(pp.name == "user_nonneg" and pp.confidence == 1.0 for pp in props)


def test_properties_sorted_by_confidence():
    ir = ir_of("samples/parse_duration.py::parse_duration")
    confs = [p.confidence for p in infer_spec(ir)]
    assert confs == sorted(confs, reverse=True)
