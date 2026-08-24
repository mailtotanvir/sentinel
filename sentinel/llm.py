"""Optional LLM tier: OpenRouter chat-completions client.

Model default: stealth/ox-alpha. Auth: OPENROUTER_API_KEY from the environment.
All output is treated as UNTRUSTED suggestions; predicates are validated before use.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .models import Property, TargetIR

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "stealth/ox-alpha"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


def gemini_chat(user: str, system: str | None = None, temperature: float = 0.2,
                max_tokens: int = 3000, timeout: float = 90.0,
                model: str | None = None,
                disable_thinking: bool = True) -> str:
    """Gemini generative-language REST call (GEMINI_API_KEY). Returns text or raises.

    Thinking is disabled by default: reasoning tokens share maxOutputTokens and
    otherwise truncate long code answers (observed thoughtsTokenCount >> output).
    """
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    gen_cfg: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if disable_thinking:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    body: dict = {"contents": [{"role": "user", "parts": [{"text": user}]}],
                  "generationConfig": gen_cfg}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    url = GEMINI_URL.format(model=model
                            or os.environ.get("SENTINEL_GEMINI_MODEL",
                                              DEFAULT_GEMINI_MODEL))
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if disable_thinking and e.code == 400:  # model rejects thinkingConfig
            return gemini_chat(user, system, temperature, max_tokens, timeout,
                               model, disable_thinking=False)
        raise
    cands = payload.get("candidates") or []
    parts = ((cands[0].get("content") or {}).get("parts")) if cands else []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text:
        reason = (cands[0].get("finishReason") if cands else None) or payload.get(
            "promptFeedback", {}).get("blockReason")
        raise RuntimeError(f"empty Gemini completion (finish={reason})")
    return text


class LLMClient:
    """Multi-provider client: OpenRouter (stealth/ox-alpha) with Gemini fallback.

    Provider order: SENTINEL_LLM_PROVIDER override, else OpenRouter when its key
    is present, else Gemini. On a failed primary call the other provider is tried.
    """

    def __init__(self, model: str | None = None, timeout: float = 90.0):
        self.model = model or os.environ.get("SENTINEL_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        self.available = bool(os.environ.get("OPENROUTER_API_KEY")
                              or os.environ.get("GEMINI_API_KEY"))
        self.provider_used: str | None = None

    def _order(self) -> list[str]:
        forced = os.environ.get("SENTINEL_LLM_PROVIDER", "").strip().lower()
        if forced in ("openrouter", "gemini"):
            return [forced]
        order = []
        if os.environ.get("OPENROUTER_API_KEY"):
            order.append("openrouter")
        if os.environ.get("GEMINI_API_KEY"):
            order.append("gemini")
        return order

    # -- low level -----------------------------------------------------------------
    def _chat(self, system: str, user: str) -> str:
        errors = []
        for provider in self._order():
            try:
                if provider == "openrouter":
                    out = self._openrouter_chat(system, user)
                else:
                    out = gemini_chat(user, system=system,
                                      timeout=self.timeout)
                self.provider_used = provider
                return out
            except Exception as e:  # noqa: BLE001 - try next provider
                errors.append(f"{provider}: {type(e).__name__}: {e}"[:200])
        raise RuntimeError("all LLM providers failed (" + "; ".join(errors) + ")")

    def _openrouter_chat(self, system: str, user: str) -> str:
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": 3000,
        }).encode()
        req = urllib.request.Request(
            OPENROUTER_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "HTTP-Referer": "https://sentinel.local",
                     "X-Title": "sentinel-verifier"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        msg = payload["choices"][0]["message"]
        content = msg.get("content")
        if not content and msg.get("reasoning"):
            # reasoning models may leave content null on token starvation;
            # fall back to the tail of the reasoning trace
            content = msg["reasoning"][-4000:]
        if not content:
            raise RuntimeError(f"empty completion ({payload.get('usage')})")
        return content

    @staticmethod
    def _extract_json(text: str):
        # 1) fenced block first
        m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 2) balanced-bracket scan, last candidate wins (reasoning traces put
        #    the final answer at the end)
        candidates = []
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            start = None
            depth = 0
            for i, ch in enumerate(text):
                if ch == open_ch:
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == close_ch and depth:
                    depth -= 1
                    if depth == 0 and start is not None:
                        candidates.append(text[start:i + 1])
        for cand in reversed(candidates):
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
        return None

    # -- spec suggestion -------------------------------------------------------------
    def suggest_properties(self, ir: TargetIR) -> list[Property]:
        """Ask the model for extra invariants; validate before returning."""
        system = (
            "You are a test oracle engineer. Given a Python function, propose up to 3 "
            "machine-checkable invariants. Respond with ONLY a JSON array; each element: "
            '{"name": str, "expr": str, "desc": str} where "expr" is a python boolean '
            "expression using the parameter names and 'result' for the return value. "
            "Use only comparisons, arithmetic, len(), abs(), isinstance(). No function calls."
        )
        user = f"def {ir.qualname}{ir.signature}\n\"\"\"{ir.docstring or ''}\"\"\"\n"
        raw = self._chat(system, user)
        data = self._extract_json(raw)
        props: list[Property] = []
        if isinstance(data, dict):
            data = data.get("properties") or [data]
        if not isinstance(data, list):
            return props
        for item in data[:3]:
            if not isinstance(item, dict):
                continue
            expr = str(item.get("expr", "")).strip()
            if not expr or any(tok in expr for tok in ("import", "__", "exec", "eval", "open(")):
                continue
            try:
                compile(expr, "<llm-bound>", "eval")
            except SyntaxError:
                continue
            props.append(Property(
                name="llm_" + re.sub(r"\W+", "_", str(item.get("name", "prop")))[:40],
                kind="range_bound", description=str(item.get("desc", expr))[:200],
                params={"expr": expr, "desc": str(item.get("desc", expr))[:200]},
                provenance="llm", confidence=0.4))
        return props

    # -- auto-fix loop ---------------------------------------------------------------
    def propose_patch(self, ir, finding_desc: dict) -> str | None:
        """Ask the model for a full replacement function that fixes the finding.

        Returns function source (def ...:) or None. Untrusted: caller verifies.
        """
        system = (
            "You are a precise Python bug-fixer. Given a function and a bug report, "
            "rewrite the COMPLETE function so the bug is fixed while preserving its "
            "documented contract. Keep the same name and signature. Respond with ONLY "
            "one python code block containing the full replacement function."
        )
        user = (f"Function:\n{ir.source_segment}\n\n"
                f"Bug report: {json.dumps(finding_desc)[:1500]}")
        try:
            raw = self._chat(system, user)
        except Exception:
            return None
        m = re.search(r"```(?:python)?\s*\n(.*?)```", raw, re.S)
        if not m:
            return None
        code = m.group(1)
        if f"def {ir.qualname.split('.')[-1]}" not in code:
            return None
        return code

    # -- report upgrades ---------------------------------------------------------------
    def explain_finding(self, ir: TargetIR, finding_desc: dict) -> str | None:
        try:
            system = ("You explain bugs tersely. One sentence root cause guess, one short "
                      "sentence suggested fix. Plain text.")
            user = (f"Function:\ndef {ir.qualname}{ir.signature}\n\nFinding: "
                    f"{json.dumps(finding_desc)[:1200]}")
            text = self._chat(system, user).strip()
            return text[:600] or None
        except Exception:
            return None
