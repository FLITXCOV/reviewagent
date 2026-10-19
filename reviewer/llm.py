"""LLM access layer: disk cache (deterministic reruns), exponential backoff, JSON extraction + one repair retry.
The provider call itself is injected as `backend(model, system, user, temperature) -> str`."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

Backend = Callable[[str, str, str, float], str]


class TransientError(Exception):
    """Raise from a backend for HTTP 429 / 5xx / timeouts. Optionally carry Retry-After seconds."""
    def __init__(self, msg: str = "", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class LLMUnavailable(Exception):
    """All retry attempts exhausted."""


class DailyQuotaError(LLMUnavailable):
    """Daily API quota exhausted. Not retriable — fail the node immediately."""


class LLMOutputError(Exception):
    """Model never produced parseable JSON."""


def extract_json(text: str) -> Any:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    pairs = sorted(((t.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if t.find(o) != -1))
    for i, _o, c in pairs:
        j = t.rfind(c)
        if j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON found in model output")


class LLMClient:
    def __init__(self, backend: Backend, model: str, cache_dir: str | Path,
                 max_attempts: int = 5, sleep: Callable[[float], None] = time.sleep):
        self.backend, self.model = backend, model
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts, self.sleep = max_attempts, sleep
        self.network_calls = self.cache_hits = self.retries = self.json_repairs = 0

    def _path(self, system: str, user: str, temperature: float, version: str) -> Path:
        h = hashlib.sha256(json.dumps([self.model, system, user, temperature, version]).encode()).hexdigest()
        return self.cache_dir / f"{h}.json"

    def complete(self, system: str, user: str, *, temperature: float = 0.0, version: str = "v1") -> str:
        path = self._path(system, user, temperature, version)
        if path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        for attempt in range(self.max_attempts):
            try:
                self.network_calls += 1
                text = self.backend(self.model, system, user, temperature)
                path.write_text(json.dumps({"text": text}), encoding="utf-8")   # only successes are cached
                return text
            except TransientError as e:
                if attempt == self.max_attempts - 1:
                    raise LLMUnavailable(str(e)) from e
                self.retries += 1
                self.sleep(e.retry_after if e.retry_after is not None else min(2 ** attempt, 30))
        raise LLMUnavailable("unreachable")

    def complete_json(self, system: str, user: str, *, temperature: float = 0.0, version: str = "v1") -> Any:
        text = self.complete(system, user, temperature=temperature, version=version)
        try:
            return extract_json(text)
        except ValueError as err:
            self.json_repairs += 1
            repair = (user + "\n\nYour previous reply was not valid JSON (" + str(err) +
                      "). Reply again with ONLY the JSON object, no prose, no code fences.")
            text2 = self.complete(system, repair, temperature=temperature, version=version)
            try:
                return extract_json(text2)
            except ValueError as err2:
                raise LLMOutputError(str(err2)) from err2
