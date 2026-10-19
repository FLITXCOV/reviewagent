"""Provider wiring: stdlib HTTP only (no SDKs). Returns (reviewer_client, judge_client).
Reviewer and judge must be DIFFERENT model families.

Env vars (defaults in brackets; CHECK the providers' docs, model ids change):
  GEMINI_API_KEY, GROQ_API_KEY
  REVIEWER_PROVIDER [gemini]  REVIEWER_MODEL [gemini-2.5-flash]
  JUDGE_PROVIDER    [groq]    JUDGE_MODEL    [llama-3.3-70b-versatile]
Supported providers: gemini, groq (OpenAI-compatible), openai.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable

from reviewer.llm import DailyQuotaError, LLMClient, TransientError

TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def _load_env_file() -> None:
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k not in os.environ:
                        os.environ[k] = v


_load_env_file()


def _post(url: str, headers: dict, body: dict, timeout: float = 90.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:                     # must be caught before URLError (its parent)
        raw = e.read().decode("utf-8", errors="replace")
        if e.code in TRANSIENT_HTTP:
            if "PerDay" in raw:
                raise DailyQuotaError(f"daily quota exhausted: {raw[:150]}")
            ra = e.headers.get("Retry-After") if e.headers else None
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = None
            raise TransientError(f"HTTP {e.code}", retry_after=retry_after) from e
        raise RuntimeError(f"HTTP {e.code} from {url}: {raw[:300]!r}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise TransientError(f"network error: {e}") from e


def _key(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"environment variable {name} is not set")
    return v


def gemini_backend(base_url: str = "https://generativelanguage.googleapis.com") -> Callable[[str, str, str, float], str]:
    def backend(model: str, system: str, user: str, temperature: float) -> str:
        data = _post(f"{base_url}/v1beta/models/{model}:generateContent",
                     {"x-goog-api-key": _key("GEMINI_API_KEY")},
                     {"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                      "generationConfig": {"temperature": temperature}})
        cands = data.get("candidates") or []
        parts = (cands[0].get("content", {}).get("parts") if cands else None) or []
        return "".join(p.get("text", "") for p in parts)       # "" (e.g. safety block) -> counted as failed node
    return backend


def openai_compat_backend(base_url: str, key_env: str) -> Callable[[str, str, str, float], str]:
    def backend(model: str, system: str, user: str, temperature: float) -> str:
        data = _post(f"{base_url}/chat/completions", {"Authorization": f"Bearer {_key(key_env)}"},
                     {"model": model, "temperature": temperature,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        choices = data.get("choices") or []
        return (choices[0].get("message", {}).get("content") or "") if choices else ""
    return backend


def backend_for(provider: str):
    p = provider.lower()
    if p == "gemini":
        return gemini_backend()
    if p == "groq":
        return openai_compat_backend("https://api.groq.com/openai/v1", "GROQ_API_KEY")
    if p == "openai":
        return openai_compat_backend("https://api.openai.com/v1", "OPENAI_API_KEY")
    if p == "openrouter":
        return openai_compat_backend("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY")
    if p == "cerebras":
        return openai_compat_backend("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY")
    raise ValueError(f"unsupported provider: {provider}")


def make_clients() -> tuple[LLMClient, LLMClient]:
    rp = os.environ.get("REVIEWER_PROVIDER", "gemini")
    rm = os.environ.get("REVIEWER_MODEL", "gemini-2.5-flash")
    default_jp = "cerebras" if "CEREBRAS_API_KEY" in os.environ and "GROQ_API_KEY" not in os.environ else ("gemini" if "GEMINI_API_KEY" in os.environ and "GROQ_API_KEY" not in os.environ else "groq")
    jp = os.environ.get("JUDGE_PROVIDER", default_jp)
    default_jm = "gemini-2.5-flash-lite" if jp == "gemini" else ("llama3.3-70b" if jp == "cerebras" else "llama-3.3-70b-versatile")
    jm = os.environ.get("JUDGE_MODEL", default_jm)

    # If OpenRouter is configured and providers not set, use verified free coding + reasoning models
    if "OPENROUTER_API_KEY" in os.environ and "REVIEWER_PROVIDER" not in os.environ and "GEMINI_API_KEY" not in os.environ:
        rp, rm = "openrouter", os.environ.get("REVIEWER_MODEL", "cohere/north-mini-code:free")
        jp, jm = "openrouter", os.environ.get("JUDGE_MODEL", "nvidia/nemotron-3.5-lightning:free")

    if rp == jp:
        if rp in {"gemini", "openrouter"}:
            fam1 = rm.split("/")[0] if "/" in rm else rm
            fam2 = jm.split("/")[0] if "/" in jm else jm
            if rm == jm or (rp == "openrouter" and fam1.lower() == fam2.lower()):
                raise RuntimeError("reviewer and judge must be different providers/model tiers")
        else:
            raise RuntimeError("reviewer and judge must be different providers/model families")

    return (LLMClient(backend_for(rp), rm, "cache/reviewer", max_attempts=8),
            LLMClient(backend_for(jp), jm, "cache/judge", max_attempts=8))
