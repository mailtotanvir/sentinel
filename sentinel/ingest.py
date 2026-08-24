"""Ingestor: AST-based extraction of targets into TargetIR."""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass

from .models import Param, TargetIR

SIDE_EFFECT_DECORATORS = {"route", "task", "app.route", "celery.task", "fixture", "given"}


@dataclass
class Selection:
    path: str
    qualname: str | None  # None => all top-level functions


def parse_selection(spec: str) -> Selection:
    """Parse 'file.py', 'file.py::func' or 'file.py::Class.method'."""
    if "::" in spec:
        path, qualname = spec.split("::", 1)
    else:
        path, qualname = spec, None
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"target file not found: {path}")
    return Selection(path=path, qualname=qualname or None)


def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _render_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, list[Param]]:
    a = node.args
    params: list[Param] = []
    parts: list[str] = []

    def add(args, kind):
        defaults = a.defaults
        n_pos = len(a.posonlyargs) + len(a.args)
        for i, arg in enumerate(args):
            if kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD"):
                di = i - (n_pos - len(defaults))
                has_def = di >= 0
                default_repr = _unparse(defaults[di]) if has_def else None
            else:
                # kwonly defaults align by index
                ki = args.index(arg)
                has_def = ki < len(a.kw_defaults) and a.kw_defaults[ki] is not None
                default_repr = _unparse(a.kw_defaults[ki]) if has_def else None
            params.append(Param(
                name=arg.arg, annotation=_unparse(arg.annotation),
                default_repr=default_repr, has_default=bool(has_def), kind=kind,
            ))
            s = arg.arg
            if arg.annotation is not None:
                s += f": {_unparse(arg.annotation)}"
            if has_def and default_repr is not None:
                s += f"={default_repr}"
            parts.append(s)

    if a.posonlyargs:
        add(a.posonlyargs, "POSITIONAL_ONLY")
        parts.append("/")
    add(a.args, "POSITIONAL_OR_KEYWORD")
    if a.vararg:
        params.append(Param(a.vararg.arg, _unparse(a.vararg.annotation), kind="VAR_POSITIONAL"))
        parts.append(f"*{a.vararg.arg}")
    elif a.kwonlyargs:
        parts.append("*")
    add(a.kwonlyargs, "KEYWORD_ONLY")
    if a.kwarg:
        params.append(Param(a.kwarg.arg, _unparse(a.kwarg.annotation), kind="VAR_KEYWORD"))
        parts.append(f"**{a.kwarg.arg}")

    ret = _unparse(node.returns)
    sig = "(" + ", ".join(parts) + ")"
    if ret:
        sig += f" -> {ret}"
    return sig, params


def _find_function(tree: ast.Module, qualname: str):
    parts = qualname.split(".")
    node = None
    for child in tree.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == parts[0]:
            node = child
            break
        if isinstance(child, ast.ClassDef) and child.name == parts[0] and len(parts) > 1:
            for sub in child.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == parts[1]:
                    node = sub
                    break
            break
    return node


def ingest_file(path: str) -> tuple[list[str], str]:
    """Return (top-level function names, module source)."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    names = [c.name for c in tree.body if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return names, src


def build_target_ir(selection: Selection) -> list[TargetIR]:
    names, src = ingest_file(selection.path)
    lines = src.splitlines()
    tree = ast.parse(src)

    def make_ir(node) -> TargetIR:
        sig, params = _render_signature(node)
        seg = "\n".join(lines[node.lineno - 1: node.end_lineno])
        decorators = [_unparse(d.decorator) or "?" for d in node.decorator_list]
        ir = TargetIR(
            module_path=selection.path,
            qualname=node.name,
            kind="async_function" if isinstance(node, ast.AsyncFunctionDef) else (
                "method" if "." in (selection.qualname or "") else "function"),
            signature=sig,
            params=params,
            return_annotation=_unparse(node.returns),
            docstring=ast.get_docstring(node),
            source_segment=seg,
            module_functions=[n for n in names if n != node.name],
            decorators=decorators,
        )
        if isinstance(node, ast.AsyncFunctionDef):
            ir.skip_reason = "async targets unsupported in v1"
        elif astpretty_is_generator(node):
            ir.skip_reason = "generator functions unsupported in v1"
        else:
            for dec in decorators:
                base = dec.split("(")[0].split(".")[-1]
                if base in SIDE_EFFECT_DECORATORS or dec in SIDE_EFFECT_DECORATORS:
                    ir.skip_reason = f"side-effect decorator: {dec}"
                    break
        return ir

    def astpretty_is_generator(node) -> bool:
        return any(
            isinstance(n, (ast.Yield, ast.YieldFrom))
            for n in ast.walk(node)
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        )

    if selection.qualname:
        node = _find_function(tree, selection.qualname)
        if node is None:
            raise LookupError(f"function {selection.qualname!r} not found in {selection.path}")
        targets = [make_ir(node)]
    else:
        targets = [make_ir(c) for c in tree.body
                   if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return targets


def declared_exceptions(ir: TargetIR) -> list[str]:
    """Static scan of raise statements inside the target -> declared exception names."""
    try:
        node_tree = ast.parse(ir.source_segment)
    except SyntaxError:
        return []
    found: list[str] = []

    class V(ast.NodeVisitor):
        def visit_Raise(self, n: ast.Raise):
            if n.exc is not None:
                exc = n.exc
                if isinstance(exc, ast.Call):
                    exc = exc.func
                name = _unparse(exc)
                if name:
                    found.append(name.split("(")[0])
            self.generic_visit(n)

    V().visit(node_tree)
    seen, out = set(), []
    for e in found:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out
