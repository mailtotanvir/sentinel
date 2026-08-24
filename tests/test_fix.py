"""Auto-fix loop tests with stub LLM clients (no network)."""

from sentinel import orchestrator


class StubLLM:
    available = True

    def __init__(self, patch_source):
        self.patch_source = patch_source

    def propose_patch(self, ir, finding_desc):
        return self.patch_source


GOOD_PATCH = open("samples/correct_parse_duration.py").read().split('"""', 2)[2].strip()
NOOP_PATCH = None
BAD_PATCH = open("samples/parse_duration.py").read().split('"""', 2)[2].strip()


def test_fix_fixed_with_good_patch(tmp_path):
    results = orchestrator.fix_findings(
        "samples/parse_duration.py::parse_duration",
        out_dir=str(tmp_path), budget_s=12.0, max_executions=250, seed=7,
        llm_client=StubLLM(GOOD_PATCH))
    assert results, "expected at least one finding to fix"
    statuses = [r["status"] for r in results]
    assert "FIXED" in statuses, statuses
    fixed = [r for r in results if r["status"] == "FIXED"][0]
    assert "patch_file" in fixed
    # saved patch must equal the good implementation's function body
    saved = open(fixed["patch_file"]).read()
    assert "def parse_duration" in saved


def test_fix_not_fixed_with_noop_patch(tmp_path):
    results = orchestrator.fix_findings(
        "samples/parse_duration.py::parse_duration",
        budget_s=12.0, max_executions=250, seed=7,
        llm_client=StubLLM(BAD_PATCH))  # re-proposes identical buggy code
    assert results
    assert all(r["status"] in ("NOT_FIXED", "NO_PATCH") for r in results), \
        [r["status"] for r in results]


def test_fix_requires_llm():
    class NoLLM:
        available = False

    try:
        orchestrator.fix_findings(
            "samples/parse_duration.py::parse_duration", llm_client=NoLLM())
        raised = False
    except RuntimeError:
        raised = True
    assert raised
