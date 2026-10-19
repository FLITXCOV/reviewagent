"""Runs one experiment arm over the cases and scores it.
Arms = (mode, retrieval). Provider wiring lives in providers.py (you implement make_clients)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.dataset import checkout, ground_truth
from eval.scoring import format_metric, score_buggy_case, score_clean_case, summarize
from reviewer.llm import LLMClient
from reviewer.pipeline import Judge, run_review

ARMS = {
    "baseline":         ("baseline", "none"),
    "multicat_none":    ("multicat", "none"),
    "multicat_symbol":  ("multicat", "symbol"),
    "multinode_none":   ("multinode", "none"),
    "multinode_symbol": ("multinode", "symbol"),
}


def load_cases(cases_dir: str | Path, split: str = "test") -> list[dict]:
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(cases_dir).glob("*.json"))
             if not p.name.startswith("_")]
    return [c for c in cases if c["split"] == split]


def run_arm(arm: str, cases: list[dict], repos_dir: str | Path, reviewer: LLMClient,
            judge_client: LLMClient | None) -> dict:
    mode, retrieval = ARMS[arm]
    judge = Judge(judge_client) if judge_client else None
    buggy_res, clean_res, per_case = [], [], []
    for c in cases:
        repo = Path(repos_dir) / c["project"]
        use_repo = retrieval == "symbol"      # no repo needed (or touched) when retrieval is off
        # BUGGY case: reviewer sees the reversed fix; snapshot = buggy commit (post-change state)
        if use_repo:
            checkout(repo, c["buggy_commit"])
        b = run_review(c["buggy_diff"], reviewer, mode=mode, retrieval=retrieval,
                       repo_root=str(repo) if use_repo else None)
        buggy_res.append(score_buggy_case(c["case_id"], b.findings, ground_truth(c), judge))
        # CLEAN control: reviewer sees the real fix; snapshot = fixed commit
        if use_repo:
            checkout(repo, c["fixed_commit"])
        k = run_review(c["clean_diff"], reviewer, mode=mode, retrieval=retrieval,
                       repo_root=str(repo) if use_repo else None)
        clean_res.append(score_clean_case(c["case_id"], k.findings))
        per_case.append({"case_id": c["case_id"], "stratum": c["stratum"],
                         "buggy": b.model_dump(), "clean": k.model_dump()})
    summary = summarize(buggy_res, clean_res)
    summary["arm"] = arm
    summary["reviewer_model"] = reviewer.model
    summary["judge_model"] = judge_client.model if judge_client else None
    summary["llm_calls"] = sum(p["buggy"]["llm_calls"] + p["clean"]["llm_calls"] for p in per_case)
    summary["retries"] = sum(p["buggy"]["retries"] + p["clean"]["retries"] for p in per_case)
    summary["failed_nodes"] = sum(len(p["buggy"]["failed_nodes"]) + len(p["clean"]["failed_nodes"]) for p in per_case)
    summary["hunk_splits"] = sum(p["buggy"]["hunk_splits"] + p["clean"]["hunk_splits"] for p in per_case)
    summary["judge_errors"] = judge.errors if judge else None
    return {"summary": summary, "per_case": per_case}


def render_summary(s: dict) -> str:
    lines = [f"== {s['arm']} (buggy n={s['n_buggy']}, clean n={s['n_clean']}) ==",
             format_metric("recall (lenient)", s["recall_lenient"])]
    if s["recall_strict"]:
        lines.append(format_metric("recall (strict, judged)", s["recall_strict"]))
    for st, v in s["by_stratum"].items():
        lines.append(format_metric(f"  {st} lenient", v["lenient"]))
        if v["strict"]:
            lines.append(format_metric(f"  {st} strict", v["strict"]))
    lines.append(format_metric("false-positive rate (clean)", s["fp_rate_clean"]))
    if s["precision_strict"]:
        lines.append(format_metric("precision (strict)", s["precision_strict"]))
    lines.append(f"findings/clean diff: {s['findings_per_clean_diff']:.2f} | llm calls: {s['llm_calls']} | "
                 f"retries: {s['retries']} | failed nodes: {s['failed_nodes']} | hunk splits: {s['hunk_splits']} | "
                 f"judge errors: {s['judge_errors']}")
    lines.append(f"reviewer model: {s.get('reviewer_model')} | judge model: {s.get('judge_model')}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="data/cases")
    ap.add_argument("--repos", default="data/repos")
    ap.add_argument("--arm", choices=sorted(ARMS), required=True)
    ap.add_argument("--split", choices=["dev", "test"], default="test")
    ap.add_argument("--limit", type=int, default=0, help="first N cases only (smoke runs)")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    from providers import make_clients            # YOU implement: returns (reviewer_client, judge_client)
    reviewer, judge_client = make_clients()
    cases = load_cases(a.cases, a.split)
    cases = cases[:a.limit] if a.limit else cases
    result = run_arm(a.arm, cases, a.repos, reviewer, judge_client)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.arm}.{a.split}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(render_summary(result["summary"]))


if __name__ == "__main__":
    main()
