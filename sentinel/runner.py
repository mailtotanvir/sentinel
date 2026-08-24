"""Runner: parent-side worker lifecycle, IPC framing, budgets, timeouts."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from .models import ExecResult

READY_TIMEOUT = 20.0


class WorkerDied(Exception):
    pass


class Runner:
    """Manages a persistent worker subprocess for one target."""

    def __init__(self, target_file: str, qualname: str, param_names=None,
                 per_call_timeout: float = 2.0):
        self.target_file = os.path.abspath(target_file)
        self.qualname = qualname
        self.param_names = param_names or []
        self.per_call_timeout = per_call_timeout
        self.executions = 0
        self.timeouts = 0
        self.respawn_count = 0
        self._proc = None
        self._q = None
        self._req_id = 0
        last_err: Exception | None = None
        for attempt in range(3):  # transient spawn failures happen under load
            try:
                self.spawn()
                return
            except RuntimeError as e:
                last_err = e
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"worker failed to start after retries: {last_err}")

    # -- lifecycle -------------------------------------------------------------
    def spawn(self) -> None:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PYTHONPATH": os.path.dirname(self.target_file),
            "HOME": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "sentinel.worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True, env=env, text=True, bufsize=1)
        self._q: queue.Queue = queue.Queue()
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()
        init = {"file": self.target_file, "func": self.qualname,
                "param_names": self.param_names}
        try:
            resp = self._roundtrip(init, READY_TIMEOUT)
        except WorkerDied as e:
            raise RuntimeError(f"worker failed to start: {e}") from e
        if resp is None:
            self.kill()
            raise RuntimeError("worker failed to initialize (timeout)")
        if "fatal" in resp:
            self.kill()
            raise RuntimeError(f"worker fatal: {resp['fatal']}")

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            for line in self._proc.stdout:
                self._q.put(line)
        except Exception:
            pass
        finally:
            self._q.put(None)

    def _roundtrip(self, payload: dict, timeout: float):
        assert self._proc.stdin
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        try:
            line = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            raise WorkerDied("worker exited")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            # resync: skip garbage lines for up to timeout
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    line = self._q.get(timeout=deadline - time.time())
                except queue.Empty:
                    return None
                if line is None:
                    raise WorkerDied("worker exited")
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    def kill(self) -> None:
        p = self._proc
        if p is None:
            return
        try:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    p.kill()
            p.wait(timeout=5)
        except Exception:
            pass
        self._proc = None

    def respawn(self) -> None:
        self.kill()
        self.respawn_count += 1
        self.spawn()

    def close(self) -> None:
        self.kill()

    # -- request helpers ---------------------------------------------------------
    def request(self, payload: dict, timeout: float | None = None):
        """Send request; returns dict response or None on per-call timeout."""
        if self._proc is None or self._proc.poll() is not None:
            self.respawn()  # lazy respawn after external kill / worker death
        timeout = timeout or self.per_call_timeout
        self._req_id += 1
        payload["id"] = self._req_id
        try:
            resp = self._roundtrip(payload, timeout + 5.0)
        except WorkerDied:
            resp = None
        if resp is None:
            # hard kill: hung target or crashed protocol
            self.kill()
            self.timeouts += 1
            return None
        return resp

    # -- public API ---------------------------------------------------------------
    def execute(self, args, kwargs=None, timeout=None, with_coverage=False) -> ExecResult:
        kwargs = kwargs or {}
        self.executions += 1
        resp = self.request({"mode": "call", "args": _jsonable(args),
                             "kwargs": _jsonable(kwargs),
                             "with_coverage": with_coverage}, timeout)
        return _to_exec_result(resp)

    def check(self, args, kwargs, properties, timeout=None, with_coverage=False):
        """Returns (ExecResult, violations list)."""
        kwargs = kwargs or {}
        self.executions += 1
        resp = self.request({"mode": "check", "args": _jsonable(args),
                             "kwargs": _jsonable(kwargs),
                             "properties": properties,
                             "with_coverage": with_coverage}, timeout)
        if resp is None:
            return ExecResult(status="TIMEOUT",
                              exc_repr=f"no response within {timeout or self.per_call_timeout}s"), []
        result = _to_exec_result(resp)
        return result, resp.get("violations") or []

    def replay_coverage(self, calls, timeout=120.0) -> dict:
        resp = self.request({"mode": "replay_coverage", "calls": [
            {"args": _jsonable(c.get("args") or []),
             "kwargs": _jsonable(c.get("kwargs") or {})} for c in calls]}, timeout)
        if not resp or "branch_pct" not in resp:
            return {"branch_pct": 0.0, "line_pct": 0.0, "executed": 0, "arcs_count": 0}
        return {k: resp[k] for k in ("branch_pct", "line_pct", "executed", "arcs_count")}


def _jsonable(vals):
    out = []
    for v in vals:
        try:
            json.dumps(v)
            out.append(v)
        except (TypeError, ValueError):
            out.append(repr(v))
    return out


def _to_exec_result(resp) -> ExecResult:
    if resp is None:
        return ExecResult(status="TIMEOUT")
    return ExecResult(
        status=resp.get("status", "INTERNAL"),
        value_repr=resp.get("value_repr"),
        exc_type=resp.get("exc_type"),
        exc_repr=resp.get("exc_repr"),
        traceback_tail=resp.get("traceback_tail") or [],
        arcs=[tuple(a) for a in (resp.get("arcs") or [])],
    )
