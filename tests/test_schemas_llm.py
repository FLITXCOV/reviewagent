import json
import pytest
from reviewer.llm import LLMClient, LLMOutputError, LLMUnavailable, TransientError, extract_json
from reviewer.schemas import Finding, coerce_findings


def test_coerce_findings_repairs_and_drops():
    raw = [
        {"file": "./a.py", "line_start": 9, "line_end": 3, "category": "LOGIC", "severity": "High", "confidence": 7, "message": "m"},
        {"file": "a.py", "line_start": 1, "category": "style", "severity": "low", "confidence": .5, "message": "bad category"},
        {"file": "a.py", "line_start": 1, "category": "logic", "severity": "low", "confidence": .5, "message": ""},
        "not a dict",
    ]
    ok, dropped = coerce_findings(raw)
    assert dropped == 3 and len(ok) == 1
    f = ok[0]
    assert (f.file, f.line_start, f.line_end, f.category, f.severity, f.confidence) == ("a.py", 3, 9, "logic", "high", 1.0)
    assert coerce_findings({"x": 1}) == ([], 0)


def test_finding_validator():
    with pytest.raises(Exception):
        Finding(file="a", line_start=5, line_end=2, category="logic", severity="low", confidence=.5, message="m")


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure! Here it is: {"a": [1, 2]} hope that helps', {"a": [1, 2]}),
    ('Result: [{"a": 1}]', [{"a": 1}]),
    ('prefix [{"a": 1}, {"b": 2}] suffix', [{"a": 1}, {"b": 2}]),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_extract_json_failure():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_cache_hit_and_key_sensitivity(tmp_path):
    calls = []
    def backend(model, system, user, t):
        calls.append(user); return "ok:" + user
    c = LLMClient(backend, "m1", tmp_path)
    assert c.complete("s", "u") == "ok:u" and c.complete("s", "u") == "ok:u"
    assert len(calls) == 1 and c.cache_hits == 1
    c.complete("s", "u2"); c.complete("s", "u", version="v2")
    assert len(calls) == 3
    assert LLMClient(backend, "m2", tmp_path).complete("s", "u") == "ok:u" and len(calls) == 4


def test_backoff_sequence_then_unavailable_and_not_cached(tmp_path):
    sleeps, n = [], {"n": 0}
    def backend(*a):
        n["n"] += 1; raise TransientError("429")
    c = LLMClient(backend, "m", tmp_path, max_attempts=5, sleep=sleeps.append)
    with pytest.raises(LLMUnavailable):
        c.complete("s", "u")
    assert sleeps == [1, 2, 4, 8] and n["n"] == 5 and not list(tmp_path.glob("*.json"))


def test_retry_after_honoured_then_success(tmp_path):
    sleeps, state = [], {"n": 0}
    def backend(*a):
        state["n"] += 1
        if state["n"] == 1:
            raise TransientError("429", retry_after=7.5)
        return "fine"
    c = LLMClient(backend, "m", tmp_path, sleep=sleeps.append)
    assert c.complete("s", "u") == "fine" and sleeps == [7.5] and c.retries == 1


def test_non_transient_error_propagates(tmp_path):
    def backend(*a): raise KeyError("bug")
    with pytest.raises(KeyError):
        LLMClient(backend, "m", tmp_path).complete("s", "u")


def test_complete_json_repair_once(tmp_path):
    outs = iter(["garbage", '{"ok": true}'])
    c = LLMClient(lambda *a: next(outs), "m", tmp_path)
    assert c.complete_json("s", "u") == {"ok": True} and c.json_repairs == 1


def test_complete_json_gives_up(tmp_path):
    c = LLMClient(lambda *a: "garbage", "m", tmp_path)
    with pytest.raises(LLMOutputError):
        c.complete_json("s", "u")
