"""Naive-prompt baseline: same model (stealth/ox-alpha) via the opencode CLI."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

BASELINE_MODEL = "openrouter/stealth/ox-alpha"  # opencode provider/model format


def build_prompt(func_name: str, signature_src: str, module_stem: str) -> str:
    return (
        f"Write thorough unit tests for the Python function `{func_name}` below using pytest.\n"
        f"The function lives in a module named `{module_stem}` (file {module_stem}.py in the "
        f"same directory as your test file). Import it exactly like:\n\n"
        f"from {module_stem} import {func_name}\n\n"
        "Cover normal cases, edge cases, boundary values and error handling. Use plain "
        "asserts. Do NOT define the function yourself. "
        "Do not read files, run commands, or use any tools - answer from the source below only.\n"
        "Respond with ONLY ONE python code block containing the complete test file.\n\n"
        f"```python\n{signature_src}\n```"
    )


def _compiles(code: str) -> bool:
    try:
        compile(code, "<baseline>", "exec")
        return True
    except SyntaxError:
        return False


def repair_unterminated_strings(code: str) -> str | None:
    """Fix string literals broken across lines by upstream markdown mangling
    (observed: newline injected after \\t escapes). Uses SyntaxError locations."""
    for _ in range(40):
        try:
            compile(code, "<repair>", "exec")
            return code
        except SyntaxError as e:
            if "unterminated string literal" not in str(e):
                return None
            lines = code.splitlines(True)
            i = (e.lineno or 1) - 1
            if i + 1 >= len(lines):
                return None
            lines[i] = lines[i].rstrip("\n") + lines[i + 1]
            del lines[i + 1]
            code = "".join(lines)
    return None


def generate_tests(func_name: str, source_segment: str, module_stem: str,
                   timeout: float = 240.0, rest: bool = False) -> str | None:
    """Ask stealth/ox-alpha for a naive test suite; return code or None.

    Harnesses:
      - rest=False (default): opencode CLI (`--format json`), as evaluated in sprint 1.
      - rest=True: direct OpenRouter chat-completions call with temperature=0
        (deterministic decoding; mitigates generation flakiness noted in the report).
    One automatic retry if the first response does not even parse.
    """
    prompt = build_prompt(func_name, source_segment, module_stem)
    for attempt in range(3):  # providers occasionally return empty output
        if rest:
            out = _generate_once_rest(prompt, timeout)
            code = _finalize_code(out, module_stem, func_name) if out else None
        else:
            code = _generate_once(prompt, timeout, module_stem, func_name)
        if code and not _compiles(code):
            code = repair_unterminated_strings(code)
        if code and _compiles(code):
            return code
        # generous spacing: provider quotas are per-minute on free tiers
        import time
        time.sleep((12, 30)[min(attempt, 1)] if rest else 2 * (attempt + 1))
    return None


LAST_REST_ERROR = ""
BASELINE_PROVIDERS_USED: list[str] = []


def _generate_once_rest(prompt: str, timeout: float) -> str | None:
    """Direct REST call at temperature=0; OpenRouter first, Gemini fallback."""
    global LAST_REST_ERROR
    errs: list[str] = []
    out = None
    if os.environ.get("OPENROUTER_API_KEY"):
        out = _openrouter_once(prompt, timeout)
        if out:
            BASELINE_PROVIDERS_USED.append("openrouter")
            return out
        errs.append(LAST_REST_ERROR or "empty openrouter response")
    if os.environ.get("GEMINI_API_KEY"):
        from .llm import gemini_chat
        try:
            # generous budget: this model cannot disable thinking, and reasoning
            # shares maxOutputTokens (observed truncation at small budgets)
            out = gemini_chat(prompt, system=None, temperature=0.0,
                              max_tokens=8192, timeout=timeout,
                              model=os.environ.get(
                                  "SENTINEL_GEMINI_MODEL", "gemini-3.6-flash"))
        except Exception as e:  # noqa: BLE001 - record and fall through
            errs.append(f"gemini: {type(e).__name__}: {e}"[:200])
            out = None
        else:
            if out:
                BASELINE_PROVIDERS_USED.append("gemini")
                return out
            errs.append("empty gemini response")
    elif not os.environ.get("OPENROUTER_API_KEY"):
        errs.append("no provider key set")
    LAST_REST_ERROR = " | ".join(e for e in errs if e)
    return None


def _openrouter_once(prompt: str, timeout: float) -> str | None:
    global LAST_REST_ERROR
    key = os.environ.get("OPENROUTER_API_KEY", "")
    body = json.dumps({
        "model": os.environ.get("SENTINEL_MODEL", "stealth/ox-alpha"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 3000,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://sentinel.local", "X-Title": "sentinel-baseline"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        LAST_REST_ERROR = f"HTTP {e.code}" + (
            " (rate limited)" if e.code == 429 else "")
        return None
    except (urllib.error.URLError, KeyError, TimeoutError):
        return None
    msg = payload["choices"][0].get("message") or {}
    return msg.get("content") or None


def _generate_once(prompt: str, timeout: float, module_stem: str,
                   func_name: str) -> str | None:
    """Run opencode with --format json and collect assistant text parts cleanly."""
    try:
        proc = subprocess.run(
            ["opencode", "run", "--format", "json", "-m", BASELINE_MODEL, prompt],
            capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    # Group text parts by message; an agentic opencode run may emit several
    # assistant messages (tool narration + final answer). Only the LAST
    # message is the deliverable; joining across messages corrupts content.
    messages: dict[str, list[str]] = {}
    order: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "text":
            part = evt.get("part") or {}
            mid = str(part.get("messageID"))
            if mid not in messages:
                messages[mid] = []
                order.append(mid)
            t = part.get("text")
            if t:
                messages[mid].append(t)
    if not messages:
        return None
    out = "".join(messages[order[-1]])
    return _finalize_code(out, module_stem, func_name)


def _finalize_code(out: str, module_stem: str, func_name: str) -> str | None:
    """Extract the test-file code from a model response; prefer last compiling block."""
    fences = re.findall(r"```(?:python)?\s*\n(.*?)```", out, re.S)
    candidates = [f for f in fences if f.strip()] or ([out] if "def test_" in out else [])
    best = None
    for cand in reversed(candidates):
        code = cand
        if f"from {module_stem} import" not in code:
            code = f"from {module_stem} import {func_name}\n" + code
        if _compiles(code):
            return code
        best = best or code
    return best  # may still be broken; caller retries then gives up


def run_pytest(directory: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Run pytest in a directory; returns (passed, tail_of_output)."""
    try:
        proc = subprocess.run(
            ["python3", "-m", "pytest", "-q", "-x", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=directory, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    tail = (proc.stdout or "")[-600:]
    return proc.returncode == 0, tail
