import json, os, re, shutil, pytest
from eval.check_thresholds import check
from eval.dataset import build_case, candidates, save_case
from eval.run_eval import load_cases, render_summary, run_arm
from reviewer.llm import LLMClient

ROOT, REPO = os.environ.get("BUGSINPY_ROOT"), os.environ.get("PYSNOOPER_REPO")


@pytest.mark.skipif(not (ROOT and REPO), reason="real data not configured")
def test_run_arm_end_to_end_with_fake_llms(tmp_path):
    ref = candidates(ROOT, ["PySnooper"])[0]
    case = build_case(ref, REPO, ROOT)
    save_case(case, tmp_path / "cases")
    repos = tmp_path / "repos"; repos.mkdir()
    try:
        os.symlink(REPO, repos / "PySnooper")
    except OSError:
        shutil.copytree(REPO, repos / "PySnooper")
    finding = {"file": ref.file, "line_start": 26, "line_end": 26, "category": "logic", "severity": "high",
               "confidence": .9, "message": "output_path is undefined"}
    def reviewer_backend(m, s, u, t):
        # flags only the BUGGY diff, whose ADDED ('+') line uses the undefined `output_path`;
        # the clean (fixed) diff only has `output_path` on a removed ('-') line.
        buggy = re.search(r"\+\s+with open\(output_path", u) is not None
        return json.dumps({"findings": [finding] if buggy else []})
    rev = LLMClient(reviewer_backend, "fake-gen", tmp_path / "c1")
    jud = LLMClient(lambda m, s, u, t: '{"matches_root_cause": true, "reasoning": "x"}', "fake-judge", tmp_path / "c2")
    cases = load_cases(tmp_path / "cases", "test")
    res = run_arm("multicat_symbol", cases, repos, rev, jud)
    s = res["summary"]
    assert (s["recall_lenient"]["k"], s["recall_strict"]["k"], s["recall_strict"]["n"]) == (1, 1, 1)
    assert s["fp_rate_clean"]["k"] == 0 and s["judge_errors"] == 0 and s["failed_nodes"] == 0
    assert "recall (lenient)" in render_summary(s)


def test_threshold_gate():
    summ = {"recall_lenient": {"k": 3, "n": 10}, "fp_rate_clean": {"k": 5, "n": 10}, "failed_nodes": 2}
    probs = check(summ, {"recall_lenient_min": .4, "fp_rate_clean_max": .4, "failed_nodes_max": 0})
    assert len(probs) == 3
    assert check(summ, {"recall_lenient_min": .3, "fp_rate_clean_max": .5, "failed_nodes_max": 2}) == []


def test_run_arm_without_retrieval_needs_no_repo(tmp_path):
    """Synthetic case (no git, no dataset): proves CI-style runs work offline with retrieval='none'."""
    buggy = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n def f(x):\n-    return x + 1\n+    return x - 1\n \n"
    clean = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n def f(x):\n-    return x - 1\n+    return x + 1\n \n"
    case = {"case_id": "syn-1", "project": "syn", "split": "test", "file": "m.py", "buggy_commit": "x", "fixed_commit": "y",
            "buggy_diff": buggy, "clean_diff": clean, "fix_patch": clean, "spans": [(2, 2)], "enclosing": [(1, 2)],
            "stratum": "modification"}
    finding = {"file": "m.py", "line_start": 2, "line_end": 2, "category": "logic", "severity": "high",
               "confidence": .9, "message": "sign flipped"}
    rev = LLMClient(lambda m, s, u, t: json.dumps({"findings": [finding] if re.search(r"\+\s+return x - 1", u) else []}),
                    "fake", tmp_path / "c1")
    res = run_arm("multicat_none", [case], tmp_path / "does-not-exist", rev, None)
    s = res["summary"]
    assert s["recall_lenient"]["k"] == 1 and s["recall_strict"] is None and s["fp_rate_clean"]["k"] == 0
    assert s["llm_calls"] == 2
