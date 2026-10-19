import pytest
from eval.scoring import (CaseResult, GroundTruth, cohens_kappa, format_metric, is_location_hit, metric,
                          score_buggy_case, score_clean_case, summarize, wilson_ci)
from reviewer.schemas import Finding, JudgeVerdict


def F(file="a.py", s=10, e=10, sev="high", cat="logic", conf=.9, msg="m"):
    return Finding(file=file, line_start=s, line_end=e, category=cat, severity=sev, confidence=conf, message=msg)


GT = GroundTruth("p-1", "a.py", ((100, 102),), ((90, 130),), "modification", "FIX")


def test_wilson_known_values():
    lo, hi = wilson_ci(0, 10); assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson_ci(10, 10); assert lo == pytest.approx(0.7225, abs=1e-3) and hi == 1.0
    lo, hi = wilson_ci(9, 24); assert (lo, hi) == (pytest.approx(0.2123, abs=2e-3), pytest.approx(0.5732, abs=2e-3))
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_kappa():
    assert cohens_kappa([True, True, False, False], [True, False, False, False]) == pytest.approx(0.5)
    assert cohens_kappa([True, False, True], [True, False, True]) == pytest.approx(1.0)
    assert cohens_kappa([True, True], [True, True]) is None
    with pytest.raises(ValueError):
        cohens_kappa([True], [])


def test_location_hit_rules():
    assert is_location_hit(F(s=100, e=100), GT)
    assert is_location_hit(F(s=112, e=112), GT)          # within +10 of span end
    assert is_location_hit(F(s=113, e=113), GT) is True   # inside enclosing function (90-130, 41 lines)
    assert not is_location_hit(F(s=200, e=200), GT)
    assert not is_location_hit(F(file="b.py", s=100, e=100), GT)
    assert is_location_hit(F(file="./a.py", s=100, e=100), GT)
    big = GroundTruth("p-2", "a.py", ((100, 102),), ((1, 500),), "modification", "FIX")
    assert not is_location_hit(F(s=300, e=300), big)      # huge enclosing function does NOT count


def test_two_stage_scoring_and_summary():
    judge_yes = lambda f, patch: JudgeVerdict(matches_root_cause=True)
    judge_no = lambda f, patch: JudgeVerdict(matches_root_cause=False)
    near_generic = [F(s=101, e=101, msg="add type hints")]
    assert score_buggy_case("c", near_generic, GT, None).strict is None
    r_no = score_buggy_case("c", near_generic, GT, judge_no)
    assert r_no.lenient and r_no.strict is False and r_no.n_tp == 0
    r_yes = score_buggy_case("c", near_generic, GT, judge_yes)
    assert r_yes.lenient and r_yes.strict and r_yes.n_tp == 1
    r_far = score_buggy_case("c", [F(s=400, e=400)], GT, judge_yes)
    assert r_far.lenient is False and r_far.strict is False

    clean = [score_clean_case("x", [F(sev="high")]), score_clean_case("y", []), score_clean_case("z", [F(sev="low")])]
    s = summarize([r_yes, r_no, r_far], clean)
    assert s["recall_lenient"]["k"] == 2 and s["recall_strict"]["k"] == 1 and s["recall_strict"]["n"] == 3
    assert s["fp_rate_clean"]["k"] == 1 and s["fp_rate_clean"]["n"] == 3
    assert s["findings_per_clean_diff"] == pytest.approx(2 / 3)
    assert s["precision_strict"]["k"] == 1 and s["precision_strict"]["n"] == 5
    assert "1/3" in format_metric("x", metric(1, 3))


def test_summary_without_judge_has_no_strict():
    r = score_buggy_case("c", [F(s=100, e=100)], GT, None)
    s = summarize([r], [])
    assert s["recall_strict"] is None and s["precision_strict"] is None
