"""Sprint 3b: Gemini provider fallback (stubbed HTTP, no network)."""
import json

import pytest

from sentinel import baseline as base
from sentinel import llm as llm_mod
from sentinel.llm import LLMClient


@pytest.fixture()
def clean_keys(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SENTINEL_LLM_PROVIDER", raising=False)


def test_gemini_chat_builds_request_and_parses(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return (b'{"candidates": [{"content": {"parts": '
                    b'[{"text": "hello"}]}, "finishReason": "STOP"}]}')

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setenv("GEMINI_API_KEY", "k" * 10)
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    out = llm_mod.gemini_chat("hi", system="sys", temperature=0.0)
    assert out == "hello"
    assert "gemini-3.6-flash" in captured["url"]
    assert "generativelanguage" in captured["url"]
    body = json.loads(captured["body"])
    assert body["generationConfig"]["temperature"] == 0.0
    assert body["system_instruction"]["parts"][0]["text"] == "sys"


def test_client_falls_back_to_gemini_on_openrouter_failure(clean_keys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "o" * 5)
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 5)
    client = LLMClient()

    def fail_openrouter(self, system, user):
        raise RuntimeError("HTTP 429 (rate limited)")

    calls = []

    def fake_gemini(user, system=None, temperature=0.2, max_tokens=3000,
                    timeout=90.0, model=None):
        calls.append(model)
        return "gemini says hi"

    monkeypatch.setattr(LLMClient, "_openrouter_chat", fail_openrouter)
    monkeypatch.setattr(llm_mod, "gemini_chat", fake_gemini)
    assert client._chat("sys", "user") == "gemini says hi"
    assert client.provider_used == "gemini"
    assert calls == [None] or all(m is None for m in calls)  # default model resolved inside


def test_client_order_respects_env_override(clean_keys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("SENTINEL_LLM_PROVIDER", "gemini")
    client = LLMClient()
    assert client._order() == ["gemini"]


def test_baseline_rest_uses_gemini_when_openrouter_rate_limited(clean_keys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")
    monkeypatch.setenv("GEMINI_API_KEY", "g")

    def rate_limited(prompt, timeout):
        base.LAST_REST_ERROR = "HTTP 429 (rate limited)"
        return None

    def gemini_ok(prompt, system=None, temperature=0.0, max_tokens=3000,
                  timeout=90.0, model=None):
        assert temperature == 0.0
        return "```python\nfrom m import f\ndef test_x():\n    assert True\n```"

    monkeypatch.setattr(base, "_openrouter_once", rate_limited)
    monkeypatch.setattr(llm_mod, "gemini_chat", gemini_ok)
    code = base.generate_tests("f", "def f(): pass", "m", rest=True,
                               timeout=30.0)
    assert code is not None and "def test_x" in code
    assert "gemini" in base.BASELINE_PROVIDERS_USED


def test_baseline_rest_records_failure_when_both_down(clean_keys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert base.generate_tests("f", "def f(): pass", "m", rest=True) is None
