import subprocess
import time


from sentinel.runner import Runner

HANG = "tests/fixtures/hang.py"


def test_ok_result():
    r = Runner("samples/parse_duration.py", "parse_duration", param_names=["text"])
    try:
        res = r.execute(["1h30m"], {})
        assert res.ok and res.value_repr == "5400"
    finally:
        r.close()


def test_exception_captured():
    r = Runner("samples/parse_duration.py", "parse_duration")
    try:
        res = r.execute(["garbage!"], {})
        assert res.status == "EXCEPTION" and res.exc_type == "ValueError"
    finally:
        r.close()


    def worker_count():
        ps = subprocess.run(["ps", "-eo", "pid,stat,command"],
                            capture_output=True, text=True)
        return sum(1 for ln in ps.stdout.splitlines()
                   if "-m sentinel.worker" in ln
                   and ln.split()[1] != "STAT"
                   and "Z" not in ln.split()[1])

    # scope to OUR delta: other sentinel processes may legitimately run in
    # parallel (bench/CI), so a global zero-worker assertion would be wrong
    before = worker_count()

    r = Runner(HANG, "spin", per_call_timeout=1.5)
    try:
        t0 = time.time()
        res = r.check([1], {}, [{"name": "ec", "kind": "exception_contract",
                                 "params": {}, "provenance": "template"}])[0]
        assert res.status == "TIMEOUT"
        assert time.time() - t0 < 6
        # worker was killed; runner must respawn transparently on next call
    finally:
        r.close()

    # under load the killed worker may linger briefly before reap: poll, don't snapshot
    deadline = time.time() + 3.0
    while time.time() < deadline and worker_count() > before:
        time.sleep(0.1)
    assert worker_count() <= before, "orphaned worker process left behind"


def test_worker_level_graceful_timeout():
    r = Runner(HANG, "spin", per_call_timeout=1.0)
    try:
        res1 = r.check([1], {}, [])[0]
        assert res1.status == "TIMEOUT"
        # worker survived via internal alarm; next call reuses it
        assert r.respawn_count == 0
        res2 = r.execute([0], {})
        assert res2.ok and res2.value_repr == "42"
    finally:
        r.close()


def test_lazy_respawn_after_kill():
    r = Runner("samples/parse_duration.py", "parse_duration")
    try:
        r.kill()
        res = r.execute(["2h"], {})
        assert res.ok and "-3600" in (res.value_repr or "")
        assert r.respawn_count >= 1
    finally:
        r.close()


def test_stdout_pollution_does_not_break_protocol(tmp_path):
    p = tmp_path / "loud.py"
    p.write_text(
        "def loud(x):\n"
        "    print('NOISE ' * 100)\n"
        "    import sys\n"
        "    sys.stdout.write('RAW\\n')\n"
        "    return x * 2\n"
    )
    r = Runner(str(p), "loud")
    try:
        for _ in range(3):
            res = r.execute([21], {})
            assert res.ok and res.value_repr == "42"
    finally:
        r.close()


def test_check_violation_flow():
    r = Runner("samples/parse_duration.py", "parse_duration")
    try:
        props = [{"name": "b", "kind": "range_bound",
                  "params": {"expr": "result >= 0"}, "provenance": "heuristic"}]
        res, viols = r.check(["2h"], {}, props)
        assert res.status == "OK"
        assert len(viols) == 1 and viols[0]["property"] == "b"
    finally:
        r.close()
