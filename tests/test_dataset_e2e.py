"""Real-data tests. Skipped unless BUGSINPY_ROOT (clone of soarsmu/BugsInPy) and PYSNOOPER_REPO are set."""
import json, os, pytest
from eval.dataset import assign_split, build_case, candidates, checkout, ground_truth, select_cases
from eval.scoring import is_location_hit, score_buggy_case
from reviewer.llm import LLMClient
from reviewer.pipeline import run_review
from reviewer.schemas import Finding

ROOT, REPO = os.environ.get("BUGSINPY_ROOT"), os.environ.get("PYSNOOPER_REPO")
needs_root = pytest.mark.skipif(not ROOT, reason="BUGSINPY_ROOT not set")
needs_repo = pytest.mark.skipif(not (ROOT and REPO), reason="BUGSINPY_ROOT/PYSNOOPER_REPO not set")


@needs_root
def test_selection_is_deterministic_and_sized():
    c = candidates(ROOT)
    a, b = select_cases(c), select_cases(candidates(ROOT))
    assert [x.case_id for x in a] == [x.case_id for x in b] and len(a) == 50
    assert max(sum(x.project == p for x in a) for p in {x.project for x in a}) <= 8
    split = assign_split(a)
    assert sum(v == "dev" for v in split.values()) == 10


@needs_repo
def test_build_case_coordinates_and_full_eval_loop(tmp_path):
    ref = [r for r in candidates(ROOT, ["PySnooper"])][0]
    case = build_case(ref, REPO, ROOT)
    assert case["fix_matches_dataset_patch"] and case["stratum"] == "modification"
    assert case["spans"] == [[26, 26]] or case["spans"] == [(26, 26)]
    # the buggy file really has the buggy code on the ground-truth line
    checkout(REPO, ref.buggy)
    assert "output_path" in open(os.path.join(REPO, ref.file)).read().splitlines()[25]
    gt = ground_truth(case)
    # a fake reviewer that flags the right line, reviewing the REVERSED diff, with symbol retrieval on
    finding = {"file": ref.file, "line_start": 26, "line_end": 26, "category": "logic", "severity": "high",
               "confidence": 0.9, "message": "output_path is undefined"}
    c = LLMClient(lambda m, s, u, t: json.dumps({"findings": [finding]}), "fake", tmp_path)
    out = run_review(case["buggy_diff"], c, mode="multicat", retrieval="symbol", repo_root=REPO)
    assert out.findings and is_location_hit(out.findings[0], gt)
    from reviewer.schemas import JudgeVerdict
    r = score_buggy_case(case["case_id"], out.findings, gt, lambda f, p: JudgeVerdict(matches_root_cause=True))
    assert r.lenient and r.strict


@needs_root
def test_change_lines_ignores_headers_context_and_index():
    from eval.dataset import change_lines
    a = "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n"
    b = "diff --git a/x b/x\nindex abcdef0..1234567 100644\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@ def f():\n c0\n ctx\n-old\n+new\n"
    assert change_lines(a) == change_lines(b) == ["-old", "+new"]
