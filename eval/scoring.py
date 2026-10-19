"""Two-stage scoring: Stage 1 deterministic location match, Stage 2 cross-model judge.
All line numbers are NEW-SIDE. For a 'buggy' case that is the line numbering of the BUGGY file."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from reviewer.schemas import Finding, JudgeVerdict

Span = tuple[int, int]
JudgeFn = Callable[[Finding, str], JudgeVerdict]


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion (returns fractions in [0,1])."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def cohens_kappa(a: Sequence[bool], b: Sequence[bool]) -> float | None:
    """Cohen's kappa for two binary raters. None when undefined (chance agreement == 1)."""
    if len(a) != len(b) or not a:
        raise ValueError("need two equal-length, non-empty label lists")
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def metric(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "pct": (100.0 * k / n) if n else 0.0, "ci_low": 100 * lo, "ci_high": 100 * hi}


def format_metric(label: str, m: dict) -> str:
    return f"{label:<26} {m['k']:>3}/{m['n']:<3} ({m['pct']:5.1f}%)  95% CI [{m['ci_low']:4.1f}%, {m['ci_high']:5.1f}%]"


@dataclass(frozen=True)
class GroundTruth:
    bug_id: str
    file: str
    spans: tuple[Span, ...]        # changed spans, new-side numbering of the BUGGY file
    enclosing: tuple[Span, ...]    # smallest enclosing def/class span per changed span (may be empty)
    stratum: str                   # "deletion_only" | "modification"
    fix_patch: str                 # the real fix (forward diff) shown to the judge only


def _norm(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and a_end >= b_start


def is_location_hit(f: Finding, gt: GroundTruth, margin: int = 10, max_enclosing_len: int = 80) -> bool:
    """Stage 1. Hit if the finding overlaps any changed span +-margin, or overlaps an enclosing
    function that is short enough (<= max_enclosing_len lines) to be meaningful."""
    if _norm(f.file) != _norm(gt.file):
        return False
    for s, e in gt.spans:
        if _overlap(f.line_start, f.line_end, s - margin, e + margin):
            return True
    for s, e in gt.enclosing:
        if (e - s + 1) <= max_enclosing_len and _overlap(f.line_start, f.line_end, s, e):
            return True
    return False


@dataclass
class CaseResult:
    case_id: str
    stratum: str | None
    n_findings: int
    n_high: int
    lenient: bool | None = None        # buggy cases only
    strict: bool | None = None         # None when no judge was supplied
    n_tp: int = 0                      # findings that are strict hits


def score_buggy_case(case_id: str, findings: Sequence[Finding], gt: GroundTruth,
                     judge: JudgeFn | None) -> CaseResult:
    lenient, strict, tp = False, (False if judge else None), 0
    for f in findings:
        if not is_location_hit(f, gt):
            continue
        lenient = True
        if judge is not None and judge(f, gt.fix_patch).matches_root_cause:
            strict, tp = True, tp + 1
    return CaseResult(case_id, gt.stratum, len(findings), sum(f.severity == "high" for f in findings),
                      lenient, strict, tp)


def score_clean_case(case_id: str, findings: Sequence[Finding]) -> CaseResult:
    return CaseResult(case_id, None, len(findings), sum(f.severity == "high" for f in findings))


def summarize(buggy: Sequence[CaseResult], clean: Sequence[CaseResult]) -> dict:
    out: dict = {"n_buggy": len(buggy), "n_clean": len(clean)}
    out["recall_lenient"] = metric(sum(bool(r.lenient) for r in buggy), len(buggy))
    judged = all(r.strict is not None for r in buggy) and len(buggy) > 0
    out["recall_strict"] = metric(sum(bool(r.strict) for r in buggy), len(buggy)) if judged else None
    out["by_stratum"] = {}
    for st in sorted({r.stratum for r in buggy if r.stratum}):
        grp = [r for r in buggy if r.stratum == st]
        out["by_stratum"][st] = {
            "lenient": metric(sum(bool(r.lenient) for r in grp), len(grp)),
            "strict": metric(sum(bool(r.strict) for r in grp), len(grp)) if judged else None,
        }
    out["fp_rate_clean"] = metric(sum(r.n_high > 0 for r in clean), len(clean))
    out["findings_per_clean_diff"] = (sum(r.n_findings for r in clean) / len(clean)) if clean else 0.0
    total = sum(r.n_findings for r in list(buggy) + list(clean))
    out["precision_strict"] = metric(sum(r.n_tp for r in buggy), total) if judged else None
    return out
