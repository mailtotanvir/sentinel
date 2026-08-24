"""Worker: sandboxed subprocess executor; line-delimited JSON protocol on stdin/stdout.

Init line : {"file": abs_path, "func": qualname}
Ack       : {"ready": true} | {"fatal": "..."}
Requests  : {"id": n, "mode": "call"|"check"|"replay_coverage"|"ping", ...}
Responses : one JSON object per line on the ORIGINAL stdout fd.
Target stdout/stderr are redirected to devnull during calls so target prints
(including C-level writes) can never corrupt the framing.
"""
from __future__ import annotations

import json
import math
import os
import signal
import sys

MAX_LINE = 8 * 1024 * 1024


class WorkerTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise WorkerTimeout()


def apply_resource_limits(as_bytes: int = 2 * 1024 ** 3) -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    except Exception:
        pass


def load_target(path: str, qualname: str):
    import importlib.util
    d = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    modname = "sentinel_target_" + "".join(c if c.isalnum() else "_" for c in stem)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(modname, os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    obj = mod
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return mod, obj


SAFE_EVAL_GLOBALS = {
    "__builtins__": {},
    "abs": abs, "len": len, "min": min, "max": max, "sum": sum,
    "int": int, "float": float, "str": str, "bool": bool,
    "round": round, "sorted": sorted, "set": set, "list": list,
    "dict": dict, "tuple": tuple, "isinstance": isinstance,
}


def safe_eq(a, b) -> bool:
    try:
        if isinstance(a, float) and isinstance(b, float) \
                and math.isnan(a) and math.isnan(b):
            return True
        return bool(a == b)
    except Exception:
        try:
            return repr(a) == repr(b)
        except Exception:
            return False


def repr_list(vals) -> str:
    try:
        return "(" + ", ".join(repr(v) for v in vals) + ")"
    except Exception:
        return "<unrepr-able args>"


class Checker:
    """Evaluates properties against the live target inside the worker."""

    def __init__(self, module, func, param_names):
        self.module = module
        self.func = func
        self.param_names = param_names
        self._inverse_cache = {}
        self._expr_cache = {}

    def _resolve_inverse(self, name):
        if name not in self._inverse_cache:
            self._inverse_cache[name] = getattr(self.module, name.split(".")[-1], None)
        return self._inverse_cache[name]

    def _compile_bound(self, expr):
        if expr not in self._expr_cache:
            try:
                self._expr_cache[expr] = compile(expr, "<sentinel-bound>", "eval")
            except SyntaxError:
                self._expr_cache[expr] = None
        return self._expr_cache[expr]

    def check_all(self, props, args, kwargs, base_status, base_exc_obj=None):
        violations = []
        for p in props or []:
            found = self._check(p, args, kwargs, base_status, base_exc_obj)
            for v in found:
                v["property"] = p.get("name")
                v["kind"] = p.get("kind")
                v["provenance"] = p.get("provenance")
            violations.extend(found)
        return violations

    # -- individual property kinds -------------------------------------------------
    def _check(self, prop, args, kwargs, base_status, base_exc_obj):
        kind, params = prop.get("kind"), prop.get("params") or {}
        out = []
        if kind == "exception_contract":
            if base_status != "EXCEPTION" or base_exc_obj is None:
                return out
            declared = params.get("declared") or []
            allowed = False
            for d in declared:
                cls = getattr(self.module, str(d).split(".")[-1], None)
                if isinstance(cls, type) and isinstance(base_exc_obj, cls):
                    allowed = True
                    break
                if str(d).split(".")[-1] == type(base_exc_obj).__name__:
                    allowed = True
                    break
            if not allowed:
                out.append({
                    "expected": f"no undeclared exception (declared={sorted(map(str, declared))})",
                    "actual": f"{type(base_exc_obj).__name__}: {base_exc_obj}",
                    "detail": "undeclared exception escaped target",
                })
        elif kind == "determinism":
            if base_status != "OK":
                return out
            try:
                r1 = self.func(*args, **kwargs)
                r2 = self.func(*args, **kwargs)
            except Exception:
                return out
            if not safe_eq(r1, r2):
                out.append({"expected": "f(x) == f(x)", "actual": f"{r1!r} != {r2!r}",
                            "detail": "nondeterministic result"})
        elif kind == "idempotence":
            if base_status != "OK":
                return out
            try:
                r1 = self.func(*args, **kwargs)
                r2 = self.func(r1, **kwargs) if kwargs else self.func(r1)
            except Exception:
                return out
            if not safe_eq(r1, r2):
                out.append({"expected": "f(f(x)) == f(x)", "actual": f"{r2!r} vs {r1!r}",
                            "detail": "not idempotent"})
        elif kind == "round_trip":
            inv_name = params.get("inverse")
            inv = self._resolve_inverse(inv_name) if inv_name else None
            if inv is None or base_status != "OK" or not args:
                return out
            try:
                r1 = self.func(*args, **kwargs)
                r2 = inv(r1)
            except Exception:
                return out
            if not safe_eq(args[0], r2):
                out.append({"expected": f"{inv_name}(f(x)) == x",
                            "actual": f"{r2!r} vs {args[0]!r}", "detail": "round trip broken"})
        elif kind == "range_bound":
            code = self._compile_bound(str(params.get("expr", "")))
            if code is None or base_status != "OK":
                return out
            try:
                result = self.func(*args, **kwargs)
            except Exception:
                return out
            names = dict(zip(self.param_names, args))
            names.update(kwargs)
            names["result"] = result
            env = dict(SAFE_EVAL_GLOBALS)
            env.update({k: v for k, v in names.items() if isinstance(k, str) and not k.startswith("_")})
            try:
                ok = bool(eval(code, env))  # noqa: S307 - restricted namespace
            except Exception:
                ok = True  # broken predicate -> never a violation (FP guard)
            if not ok:
                out.append({"expected": str(params.get("desc") or params.get("expr")),
                            "actual": f"result={result!r}", "detail": "range bound violated"})
        elif kind == "metamorphic":
            # user-defined relation: transform maps args-tuple x -> new args tuple,
            # relation compares r1 = f(*x) and r2 = f(*transform(x))
            tf = self._compile_bound(str(params.get("transform", "")))
            rel = self._compile_bound(str(params.get("relation", "")))
            if tf is None or rel is None:
                return out
            try:
                r1 = self.func(*args, **kwargs)
            except Exception:
                return out
            env = dict(SAFE_EVAL_GLOBALS)
            env["x"] = tuple(args)
            try:
                new_x = eval(tf, env)  # noqa: S307 restricted namespace
            except Exception:
                return out  # transform inapplicable to this input -> skip
            if not isinstance(new_x, (tuple, list)):
                return out
            try:
                r2 = self.func(*list(new_x))
            except Exception:
                return out
            env2 = dict(SAFE_EVAL_GLOBALS)
            env2.update({"r1": r1, "r2": r2})
            try:
                holds = bool(eval(rel, env2))  # noqa: S307 restricted namespace
            except Exception:
                return out  # broken predicate -> never a violation (FP guard)
            if not holds:
                out.append({
                    "expected": params.get("desc") or str(params.get("relation")),
                    "actual": f"f{repr_list(args)}={r1!r} vs f{repr_list(list(new_x))}={r2!r}",
                    "detail": "metamorphic relation violated"})
        elif kind == "snapshot":
            mismatches = []
            for case in params.get("cases", []):
                c_args, c_kwargs = case.get("args", []), case.get("kwargs", {})
                try:
                    got_repr = repr(self.func(*c_args, **c_kwargs))
                except Exception as e:
                    got_repr = f"<EXC {type(e).__name__}>"
                if got_repr != case.get("expect_repr"):
                    mismatches.append({"args": c_args, "input": repr_list(c_args),
                                       "was": case.get("expect_repr"), "now": got_repr})
            if mismatches:
                m = mismatches[0]
                out.append({"expected": f"f{m['input']} == {m['was']}", "actual": m["now"],
                            "detail": "behavior differs from reference snapshot",
                            "case_args": m["args"], "was": m["was"]})
        return out


def _call_with_coverage(func, args, kwargs, target_file, want_arcs):
    cov = None
    if want_arcs:
        try:
            import coverage
            cov = coverage.Coverage(branch=True, data_file=None,
                                    include=[target_file], concurrency=["thread"])
            cov.start()
        except Exception:
            cov = None
    try:
        value = func(*args, **kwargs)
        status = "OK"
        exc_obj = None
        tb_tail = []
    except WorkerTimeout:
        raise
    except SystemExit as e:
        value, status, exc_obj = f"SystemExit({e})", "EXCEPTION", e
        tb_tail = []
    except RecursionError as e:
        value, status, exc_obj = None, "EXCEPTION", e
        tb_tail = traceback_tail()
    except BaseException as e:  # noqa: BLE001 - sandbox boundary
        value, status, exc_obj = None, "EXCEPTION", e
        tb_tail = traceback_tail()
    finally:
        arcs = []
        if cov is not None:
            try:
                cov.stop()
                data = cov.get_data()
                fn = data.measured_files()[0] if data.measured_files() else target_file
                arcs = [list(a) for a in (data.arcs(fn) or [])]
            except Exception:
                arcs = []
    if status == "OK":
        return {"status": "OK", "value_repr": repr(value)[:2000], "arcs": arcs}, None, value
    return ({"status": status, "value_repr": repr(value)[:500],
             "exc_type": type(exc_obj).__name__,
             "exc_repr": str(exc_obj)[:1000], "traceback_tail": tb_tail, "arcs": arcs},
            exc_obj, None)


def traceback_tail(limit=6):
    import traceback as tb
    import sys as _s
    et, ev, tr = _s.exc_info()
    try:
        frames = tb.format_exception(et, ev, tr)
    except Exception:
        return []
    tail = "".join(frames).splitlines()
    return tail[-limit:]


def handle_request(req, state):
    rid = req.get("id")
    mode = req.get("mode")
    timeout = float(req.get("timeout", 2.0))
    want_arcs = bool(req.get("with_coverage"))

    def respond(payload):
        payload["id"] = rid
        return payload

    if mode == "ping":
        return respond({"pong": True})

    if mode == "replay_coverage":
        calls = req.get("calls", [])
        metrics = replay_with_coverage(state["file"], state["func"], calls, state["checker"])
        return respond(metrics)

    args = req.get("args") or []
    kwargs = req.get("kwargs") or {}

    devnull = os.open(os.devnull, os.O_WRONLY)
    saved_out, saved_err = os.dup(1), os.dup(2)
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    base_exc = None
    try:
        payload, base_exc, value = _call_with_coverage(
            state["func"], args, kwargs, state["file"], want_arcs)
    except WorkerTimeout:
        return respond({"status": "TIMEOUT", "violations": [],
                        "exc_type": "WorkerTimeout",
                        "exc_repr": f"timeout after {timeout}s"})
    except Exception as e:  # worker-internal failure
        return respond({"status": "INTERNAL", "violations": [],
                        "exc_type": type(e).__name__, "exc_repr": str(e)[:300]})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(devnull)
        os.close(saved_out)
        os.close(saved_err)

    violations = []
    if mode == "check" and payload["status"] != "TIMEOUT":
        try:
            violations = state["checker"].check_all(
                req.get("properties"), args, kwargs, payload["status"], base_exc)
        except Exception as e:
            payload["status"] = "INTERNAL"
            payload["exc_type"] = type(e).__name__
            payload["exc_repr"] = f"checker failure: {e}"
    payload["violations"] = violations
    return respond(payload)


def replay_with_coverage(path, qualname, calls, checker):
    import coverage
    mod, func = load_target(path, qualname) if checker is None else (checker.module, checker.func)
    cov = coverage.Coverage(branch=True, data_file=None,
                            include=[os.path.abspath(path)])
    cov.start()
    executed = 0
    for c in calls:
        try:
            func(*(c.get("args") or []), **(c.get("kwargs") or {}))
            executed += 1
        except Exception:
            executed += 1
    cov.stop()
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        outfile = tf.name
    cov.json_report(outfile=outfile)
    with open(outfile) as fh:
        j = json.load(fh)
    os.unlink(outfile)
    file_summary = {}
    for fname, fdata in j.get("files", {}).items():
        s = fdata.get("summary", {})
        if os.path.abspath(fname) == os.path.abspath(path) or fname.endswith(os.path.basename(path)):
            file_summary = s
            break
    nb = file_summary.get("num_branches", 0) or 0
    cb = file_summary.get("covered_branches", 0) or 0
    ns = file_summary.get("num_statements", 0) or 0
    cl = file_summary.get("covered_lines", 0) or 0
    branch_pct = (cb / nb) if nb else 1.0
    line_pct = (cl / ns) if ns else 0.0
    arcs_union = set()
    data = cov.get_data()
    for mf in data.measured_files():
        for a in (data.arcs(mf) or []):
            arcs_union.add(tuple(a))
    return {"branch_pct": round(branch_pct, 4), "line_pct": round(line_pct, 4),
            "executed": executed, "arcs_count": len(arcs_union)}


def main():  # pragma: no cover - process entry
    apply_resource_limits()
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    devnull_w = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_w, 1)
    # anything the worker itself prints goes to stderr; stdout carries only protocol
    sys.stdout = sys.stderr

    def emit(obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        protocol.write(text[:MAX_LINE] + "\n")

    line = sys.stdin.readline()
    try:
        init = json.loads(line)
        module, func = load_target(init["file"], init["func"])
        param_names = init.get("param_names") or []
    except Exception as e:
        emit({"fatal": f"{type(e).__name__}: {e}"})
        return 3
    from .worker import Checker  # self-import safe
    state = {"file": os.path.abspath(init["file"]), "func": func,
             "checker": Checker(module, func, param_names)}
    emit({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue  # resync on garbage framing
        try:
            resp = handle_request(req, state)
        except Exception as e:  # last-resort guard
            resp = {"id": req.get("id"), "status": "INTERNAL",
                    "exc_type": type(e).__name__, "exc_repr": str(e)[:300],
                    "violations": []}
        try:
            emit(resp if isinstance(resp, str) else resp)
        except BrokenPipeError:
            return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
