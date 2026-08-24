"""Built-in AST mutation engine (deterministic, no external deps).

Spec allows mutmut OR manual mutants; built-in keeps the benchmark reproducible.
"""
from __future__ import annotations

import ast
import copy
import random

COMPARE_FLIP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
                ast.Gt: ast.GtE, ast.GtE: ast.Gt}
ARITH_FLIP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv,
              ast.FloorDiv: ast.Mult}

# Direct function-body statements eligible for deletion mutants
DELETABLE_STMTS = (ast.Assign, ast.AugAssign, ast.Expr, ast.Return, ast.Raise)


def _annotate_sites(tree: ast.Module) -> int:
    """Assign unique _sentinel_site ids to mutable nodes; return count."""
    counter = {"i": 0}

    class V(ast.NodeVisitor):
        def _skip_docstring(self, body):
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                setattr(body[0].value, "_sentinel_skip", True)

        def visit_Module(self, node):
            self._skip_docstring(node.body)
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            self._skip_docstring(node.body)
            self.generic_visit(node)
            for stmt in node.body:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                    continue  # docstring
                if isinstance(stmt, DELETABLE_STMTS):
                    setattr(stmt, "_sentinel_site", counter["i"])
                    setattr(stmt, "_sentinel_del_stmt", True)
                    counter["i"] += 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Raise(self, node):
            # exception MESSAGE text is never observed by SENTINEL's oracles
            # (they compare types); mutating it yields quasi-equivalent mutants.
            # The raised expression itself (e.g. a Compare) stays mutable.
            exc = node.exc
            if isinstance(exc, ast.Call):
                for arg in (*exc.args, *exc.keywords):
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Constant):
                            setattr(sub, "_sentinel_skip", True)
            elif isinstance(exc, ast.Constant):
                setattr(exc, "_sentinel_skip", True)
            self.generic_visit(node)

        def visit_Compare(self, node):
            self.generic_visit(node)
            for i, op in enumerate(node.ops):
                if type(op) in COMPARE_FLIP:
                    setattr(node, "_sentinel_site", counter["i"])
                    setattr(node, "_sentinel_op_idx", i)
                    counter["i"] += 1

        def visit_BinOp(self, node):
            self.generic_visit(node)
            if type(node.op) in ARITH_FLIP:
                setattr(node, "_sentinel_site", counter["i"])
                counter["i"] += 1

        def visit_AugAssign(self, node):
            self.generic_visit(node)
            if type(node.op) in ARITH_FLIP:
                setattr(node, "_sentinel_site", counter["i"])
                counter["i"] += 1

        def visit_BoolOp(self, node):
            self.generic_visit(node)
            setattr(node, "_sentinel_site", counter["i"])
            counter["i"] += 1

        def visit_Slice(self, node):
            # slice bounds are mutated by the Slice site itself (+-1 shifts);
            # marking them skipped here avoids duplicate-source collisions
            for bound in (node.lower, node.upper):
                if isinstance(bound, ast.Constant):
                    setattr(bound, "_sentinel_skip", True)
            has_const_bound = (isinstance(node.lower, ast.Constant)
                               or isinstance(node.upper, ast.Constant))
            if has_const_bound:
                setattr(node, "_sentinel_site", counter["i"])
                counter["i"] += 1
            self.generic_visit(node)

        def visit_Constant(self, node):
            self.generic_visit(node)
            if getattr(node, "_sentinel_skip", False):
                return
            v = node.value
            if isinstance(v, bool):
                return
            if isinstance(v, int) and abs(v) < 10 ** 6:
                setattr(node, "_sentinel_site", counter["i"])
                counter["i"] += 1
            elif isinstance(v, str):
                setattr(node, "_sentinel_site", counter["i"])
                counter["i"] += 1

    V().visit(tree)
    return counter["i"]


def _find_by_site(tree: ast.Module, site_id: int):
    found = {"n": None}

    class V(ast.NodeVisitor):
        def generic_visit(self, node):
            if getattr(node, "_sentinel_site", None) == site_id:
                found["n"] = node
            super().generic_visit(node)

    V().visit(tree)
    return found["n"]


class _MutSingle(ast.NodeTransformer):
    """Applies exactly one mutation to the node carrying _mutate_me."""

    def mutate(self, node):
        if getattr(node, "_sentinel_del_stmt", False):
            return None  # statement-deletion mutant
        if isinstance(node, ast.Compare):
            ops = list(node.ops)
            idx = getattr(node, "_sentinel_op_idx", 0)
            ops[idx] = COMPARE_FLIP[type(ops[idx])]()
            node.ops = ops
        elif isinstance(node, ast.BinOp):
            node.op = ARITH_FLIP[type(node.op)]()
        elif isinstance(node, ast.AugAssign):
            node.op = ARITH_FLIP[type(node.op)]()
        elif isinstance(node, ast.BoolOp):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        elif isinstance(node, ast.Slice):
            # deterministic off-by-one: upper +1 preferred, else lower -1
            u, lo = node.upper, node.lower
            if isinstance(u, ast.Constant) and isinstance(u.value, int) \
                    and not isinstance(u.value, bool):
                node.upper = ast.Constant(value=u.value + 1)
            elif isinstance(lo, ast.Constant) and isinstance(lo.value, int) \
                    and not isinstance(lo.value, bool):
                node.lower = ast.Constant(value=lo.value - 1)
        elif isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, str):
                node.value = "" if not v else (v[:-1] if len(v) > 1 else "x")
            else:
                node.value = {0: 1, 1: 0}.get(v, v + 1 if v >= 0 else v - 1)
        return node

    def generic_visit(self, node):
        super().generic_visit(node)
        if getattr(node, "_mutate_me", False):
            return self.mutate(node)
        return node


def generate_mutants(source: str, func_name: str | None = None,
                     max_mutants: int = 25, seed: int = 7) -> list[tuple[str, str]]:
    """Return [(description, mutated_source)] — one single-site mutation each."""
    base = ast.parse(source)
    n_sites = _annotate_sites(base)
    rng = random.Random(seed)
    order = list(range(n_sites))
    rng.shuffle(order)
    mutants = []
    for sid in order[: max_mutants * 2]:
        if len(mutants) >= max_mutants:
            break
        tree = copy.deepcopy(base)
        target = _find_by_site(tree, sid)
        if target is None:
            continue
        setattr(target, "_mutate_me", True)
        mt = _MutSingle()
        new_tree = mt.visit(tree)
        try:
            src = ast.unparse(new_tree)
            ast.parse(src)
        except Exception:
            continue
        lineno = getattr(target, "lineno", "?")
        if getattr(target, "_sentinel_del_stmt", False):
            label = f"Del:{type(target).__name__}"
        else:
            label = type(target).__name__
        mutants.append((f"{label}@L{lineno}#s{sid}", src))
    return mutants


def split_mutants(source: str, per_set: int = 12, seed: int = 7) -> tuple:
    """Deterministically split mutant space into two DISJOINT sets.

    Returns (primary, holdout); each has at most `per_set` entries and no
    source-identical mutants cross sets. Used for the spec §11 overfitting check.
    """
    all_muts = generate_mutants(source, max_mutants=10 ** 9, seed=seed)
    seen: dict[int, None] = {}
    uniq = []
    for d, s in all_muts:
        k = hash(s)
        if k not in seen:
            seen[k] = None
            uniq.append((d, s))
    rng = random.Random(seed)
    rng.shuffle(uniq)
    return uniq[:per_set], uniq[per_set:per_set * 2]


def unique_mutants(source: str) -> list[tuple[str, str]]:
    """All distinct-site mutants with source dedup, unshuffled and uncapped."""
    seen: dict[int, None] = {}
    out = []
    for d, s in generate_mutants(source, max_mutants=10 ** 9, seed=0):
        k = hash(s)
        if k not in seen:
            seen[k] = None
            out.append((d, s))
    return out
