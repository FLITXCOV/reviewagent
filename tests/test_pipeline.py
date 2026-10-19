import json
from reviewer.llm import LLMClient, TransientError
from reviewer.pipeline import Judge, batch_hunks, dedupe_and_rank, run_review
from reviewer.diffparse import parse_unified_diff
from reviewer.schemas import Finding

DIFF = """diff --git a/pkg/pay.py b/pkg/pay.py
--- a/pkg/pay.py
+++ b/pkg/pay.py
@@ -8,4 +8,4 @@ class Pay:
     def process_payment(self, t):
-        if not validate_token(t):
+        if validate_token(t):
             raise ValueError
         return helper()
"""

FINDING = {"file": "pkg/pay.py", "line_start": 9, "line_end": 9, "category": "logic", "severity": "high",
           "confidence": 0.9, "message": "condition inverted: valid tokens raise ValueError"}


class Fake:
    def __init__(self, findings=(FINDING,)):
        self.prompts, self.findings = [], list(findings)
    def __call__(self, model, system, user, t):
        self.prompts.append(user)
        if system.startswith("You summarise"):
            return "The change flips a condition."
        return json.dumps({"findings": self.findings})


def client(tmp_path, backend, **kw):
    return LLMClient(backend, "fake", tmp_path, sleep=lambda s: None, **kw)


def test_call_counts_per_mode(tmp_path):
    for mode, calls in (("baseline", 1), ("multicat", 1), ("multinode", 5)):   # 1 summary + 4 category nodes
        fake = Fake()
        out = run_review(DIFF, client(tmp_path / mode, fake), mode=mode)
        assert len(fake.prompts) == calls, mode
        assert out.llm_calls == calls and out.findings and out.failed_nodes == []


def test_findings_deduped_across_nodes(tmp_path):
    out = run_review(DIFF, client(tmp_path, Fake()), mode="multinode")
    assert len(out.findings) == 1        # same finding returned by 4 nodes -> 1


def test_symbol_retrieval_adds_context_only_when_asked(tmp_path):
    repo = tmp_path / "repo" / "pkg"
    repo.mkdir(parents=True)
    (repo / "pay.py").write_text("def validate_token(t):\n    return bool(t)\ndef helper():\n    return 1\n\n"
                                 "class Pay:\n    def process_payment(self, t):\n        x = 1\n"
                                 "        if validate_token(t):\n            raise ValueError\n        return helper()\n")
    f1, f2 = Fake(), Fake()
    run_review(DIFF, client(tmp_path / "c1", f1), mode="multicat", retrieval="none")
    run_review(DIFF, client(tmp_path / "c2", f2), mode="multicat", retrieval="symbol", repo_root=str(tmp_path / "repo"))
    assert "Related code from the repository" not in f1.prompts[0]
    assert "validate_token" in f2.prompts[0] and "Related code from the repository" in f2.prompts[0]
    f3 = Fake()
    run_review(DIFF, client(tmp_path / "c3", f3), mode="baseline", retrieval="symbol", repo_root=str(tmp_path / "repo"))
    assert "Related code from the repository" not in f3.prompts[0]      # baseline never gets retrieval


def test_total_api_failure_is_survivable(tmp_path):
    def down(*a): raise TransientError("503")
    out = run_review(DIFF, client(tmp_path, down, max_attempts=2), mode="multinode")
    assert out.findings == [] and "summary" in out.failed_nodes and len(out.failed_nodes) == 5


def test_garbage_output_is_survivable_and_counted(tmp_path):
    bad = lambda *a: "I refuse to output JSON"
    out = run_review(DIFF, client(tmp_path, bad), mode="multicat")
    assert out.failed_nodes == ["multicat[0]"] and out.retries == 1


def test_invalid_findings_dropped_and_counted(tmp_path):
    fake = Fake(findings=[FINDING, {"file": "x", "line_start": 1, "category": "style"}])
    out = run_review(DIFF, client(tmp_path, fake), mode="multicat")
    assert len(out.findings) == 1 and out.dropped_findings == 1


def test_empty_or_non_python_diff_makes_no_calls(tmp_path):
    fake = Fake()
    out = run_review("diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -1 +1 @@\n-a\n+b\n", client(tmp_path, fake))
    assert out.findings == [] and fake.prompts == []


def test_batching_splits_large_diffs():
    h = parse_unified_diff(DIFF)[0].hunks[0]
    assert len(batch_hunks([h] * 5, max_chars=10 ** 6)) == 1
    assert len(batch_hunks([h] * 5, max_chars=len(DIFF) + 50)) > 1


def test_dedupe_rank_filter():
    mk = lambda s, c, sev="low", cat="logic": Finding(file="a.py", line_start=s, line_end=s, category=cat,
                                                      severity=sev, confidence=c, message="m")
    got = dedupe_and_rank([mk(10, .6), mk(12, .9), mk(50, .4), mk(80, .7, "high")], min_conf=.5)
    assert [(f.line_start, f.severity) for f in got] == [(80, "high"), (12, "low")]


def test_judge_never_raises(tmp_path):
    ok = Judge(client(tmp_path / "a", lambda *a: '{"matches_root_cause": true, "reasoning": "same"}'))
    assert ok(Finding(**FINDING), "FIX").matches_root_cause is True
    bad = Judge(client(tmp_path / "b", lambda *a: "nope"))
    v = bad(Finding(**FINDING), "FIX")
    assert v.matches_root_cause is False and bad.errors == 1
