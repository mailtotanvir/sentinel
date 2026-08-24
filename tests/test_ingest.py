import textwrap

import pytest

from sentinel.ingest import build_target_ir, parse_selection, declared_exceptions


def write(tmp_path, src):
    p = tmp_path / "mod.py"
    p.write_text(textwrap.dedent(src))
    return str(p)


def test_selection_parsing(tmp_path):
    p = write(tmp_path, "def f():\n    pass\n")
    sel = parse_selection(p + "::f")
    assert sel.qualname == "f"
    bare = parse_selection(p)
    assert bare.qualname is None
    with pytest.raises(FileNotFoundError):
        parse_selection("/nonexistent/xyz.py::f")


def test_signature_and_params(tmp_path):
    p = write(tmp_path, '''
        def f(a: int, b: str = "x", *args, flag: bool = False) -> int:
            """Doc."""
            return a
    ''')
    ir = build_target_ir(parse_selection(p + "::f"))[0]
    assert ir.signature.startswith("(a: int")
    annos = {pp.name: pp.annotation for pp in ir.params}
    assert annos["a"] == "int" and annos["flag"] == "bool"
    assert ir.params[1].has_default and ir.params[1].default_repr == "'x'"
    assert ir.return_annotation == "int"
    assert ir.docstring == "Doc."


def test_async_and_generator_skipped(tmp_path):
    p = write(tmp_path, '''
        async def g(): ...
        def gen():
            yield 1
        def ok(): ...
    ''')
    targets = {t.qualname: t for t in build_target_ir(parse_selection(p))}
    assert targets["g"].skip_reason
    assert targets["gen"].skip_reason
    assert not targets["ok"].skip_reason


def test_declared_exceptions_static_scan(tmp_path):
    p = write(tmp_path, '''
        def f(x):
            if x:
                raise ValueError("bad")
            raise KeyError("k")
    ''')
    ir = build_target_ir(parse_selection(p + "::f"))[0]
    assert set(declared_exceptions(ir)) == {"ValueError", "KeyError"}


def test_module_functions_siblings(tmp_path):
    p = write(tmp_path, '''
        def encode(s): ...
        def decode(s): ...
    ''')
    ir = build_target_ir(parse_selection(p + "::encode"))[0]
    assert "decode" in ir.module_functions
