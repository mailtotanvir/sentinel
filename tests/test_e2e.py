import json
import os
import subprocess
import sys



def run_cli(*argv, timeout=300):
    env = dict(os.environ)
    return subprocess.run([sys.executable, "-m", "sentinel.cli", *argv],
                          capture_output=True, text=True, timeout=timeout, env=env)


def test_probe_mode_exit_codes():
    r = run_cli("scan", "samples/parse_duration.py::parse_duration", "--probe", "('1h30m',)")
    assert r.returncode == 0 and "5400" in r.stdout
    r = run_cli("scan", "samples/parse_duration.py::parse_duration",
                "--probe", "('not a duration!',)", )
    assert r.returncode == 1 and "ValueError" in r.stdout
    r = run_cli("scan", "samples/parse_duration.py::parse_duration", "--probe", "('2h',)")
    assert "-3600" in r.stdout


def test_scan_finds_bug_and_writes_artifacts(tmp_path):
    out = str(tmp_path / "rep")
    r = run_cli("scan", "samples/parse_duration.py::parse_duration",
                "--out", out, "--budget", "20", "--max-executions", "500",
                "--seed", "42")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "SUSPECTED" in r.stdout or "CONFIRMED" in r.stdout
    assert os.path.isfile(os.path.join(out, "report.json"))
    assert os.path.isfile(os.path.join(out, "report.md"))
    rep = json.load(open(os.path.join(out, "report.json")))
    assert rep["target"].endswith("parse_duration")
    assert any(b["severity_class"] == "SUSPECTED" for b in rep["bugs"])
    assert rep["coverage"]["branch"] >= 0.5


def test_scan_clean_target_exits_zero(tmp_path):
    r = run_cli("scan", "samples/correct_parse_duration.py::parse_duration",
                "--budget", "15", "--max-executions", "400", "--seed", "7")
    assert r.returncode == 0, r.stdout


def test_llm_flag_offline_graceful(tmp_path):
    """--llm without network/key must degrade to heuristic tier, not crash."""
    env = dict(os.environ)
    env.pop("OPENROUTER_API_KEY", None)
    r = subprocess.run([sys.executable, "-m", "sentinel.cli", "scan",
                        "samples/correct_parse_duration.py::parse_duration",
                        "--llm", "--budget", "10", "--max-executions", "200"],
                       capture_output=True, text=True, timeout=300, env=env)
    assert r.returncode == 0
    assert "INTERNAL" not in r.stdout
