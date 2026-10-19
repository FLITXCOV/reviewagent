"""ReviewAgent Interactive CLI.
Review real pull request diffs, local patches, or historical BugsInPy cases with AST symbol retrieval.

Usage:
  python review.py --case black-13
  python review.py --demo
  python review.py --diff path/to/patch.diff
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from providers import make_clients
from reviewer.pipeline import run_review
from reviewer.diffparse import parse_unified_diff, render_hunk


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Terminal color helpers (ANSI)
def _bold(s: str) -> str: return f"\033[1m{s}\033[0m"
def _red(s: str) -> str: return f"\033[91m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[93m{s}\033[0m"
def _green(s: str) -> str: return f"\033[92m{s}\033[0m"
def _cyan(s: str) -> str: return f"\033[96m{s}\033[0m"
def _dim(s: str) -> str: return f"\033[2m{s}\033[0m"


def format_cli_review(output, diff_text: str, duration: float, model_name: str) -> str:
    lines = []
    lines.append(_cyan("=" * 72))
    lines.append(_bold("  [ReviewAgent] Automated Pull Request Review"))
    lines.append(_dim(f"  Reviewer Model: {model_name} | Elapsed: {duration:.2f}s | LLM Calls: {output.llm_calls}"))
    lines.append(_cyan("=" * 72))
    lines.append("")

    if output.summary:
        lines.append(_bold("Change Summary:"))
        lines.append(f"  {output.summary.strip()}")
        lines.append("")

    if output.failed_nodes:
        lines.append(_red(f"[!] Warning: Nodes failed during review: {', '.join(output.failed_nodes)} (quota exhaustion or transient error)"))
        lines.append("")

    if not output.findings:
        if output.failed_nodes:
            lines.append(_yellow("[!] Review incomplete due to failed nodes."))
        else:
            lines.append(_green("[OK] No defects found. The code change looks clean and well-structured."))
        lines.append("")
        return "\n".join(lines)

    lines.append(_bold(f"Defect Findings ({len(output.findings)}):"))
    lines.append("")

    sev_colors = {
        "high": _red(" [HIGH] "),
        "medium": _yellow(" [MEDIUM] "),
        "low": _cyan(" [LOW] ")
    }

    for i, f in enumerate(output.findings, start=1):
        badge = sev_colors.get(f.severity.lower(), f" [{f.severity.upper()}] ")
        lines.append(f"{_bold(f'#{i}')} {badge} {_bold(f.category.upper())} at {_cyan(f.file)}:{_yellow(f'{f.line_start}-{f.line_end}')}")
        lines.append(f"   {_bold('Confidence:')} {f.confidence * 100:.0f}%")
        lines.append(f"   {_bold('Issue:')} {f.message}")
        if f.evidence:
            lines.append(f"   {_bold('Evidence in Diff:')}")
            for ev in f.evidence[:3]:
                lines.append(f"     {_dim(ev.strip())}")
        if f.suggested_fix:
            lines.append(f"   {_green('Suggested Fix:')} {f.suggested_fix}")
        lines.append(_dim("-" * 68))

    return "\n".join(lines)


def run_case_review(case_id: str, mode: str = "multicat", retrieval: str = "symbol"):
    case_file = Path("data/cases") / f"{case_id}.json"
    if not case_file.exists():
        print(_red(f"Error: Case file {case_file} not found."))
        sys.exit(1)

    case = json.loads(case_file.read_text(encoding="utf-8"))
    repo_dir = Path("data/repos") / case["project"]
    use_repo = retrieval == "symbol" and repo_dir.exists()

    reviewer, _ = make_clients()

    print(_bold(f"\n[*] Loading historical bug case: {case_id} (Project: {case['project']}, File: {case['file']})"))
    if use_repo:
        print(_dim(f"    Repository commit snapshot: {case['buggy_commit'][:10]} with AST symbol indexing"))
    
    t0 = time.time()
    out = run_review(case["buggy_diff"], reviewer, mode=mode, retrieval=retrieval,
                     repo_root=str(repo_dir) if use_repo else None)
    t1 = time.time()

    print(format_cli_review(out, case["buggy_diff"], t1 - t0, reviewer.model))


def main():
    parser = argparse.ArgumentParser(description="ReviewAgent - Agentic Python Pull Request Reviewer")
    parser.add_argument("--case", type=str, help="Case ID to review (e.g., black-13, black-12, fastapi-11)")
    parser.add_argument("--demo", action="store_true", help="Run live review demo on historical defect black-13")
    parser.add_argument("--diff", type=str, help="Path to unified diff patch file to review")
    parser.add_argument("--repo", type=str, default=None, help="Optional repo root for AST symbol retrieval")
    parser.add_argument("--mode", choices=["baseline", "multicat", "multinode"], default="multicat")
    parser.add_argument("--retrieval", choices=["none", "symbol"], default="symbol")

    args = parser.parse_args()

    if args.demo or (not args.case and not args.diff):
        # Default demo: black-13
        run_case_review("black-13", mode=args.mode, retrieval=args.retrieval)
    elif args.case:
        run_case_review(args.case, mode=args.mode, retrieval=args.retrieval)
    elif args.diff:
        diff_text = Path(args.diff).read_text(encoding="utf-8")
        reviewer, _ = make_clients()
        t0 = time.time()
        out = run_review(diff_text, reviewer, mode=args.mode, retrieval=args.retrieval, repo_root=args.repo)
        t1 = time.time()
        print(format_cli_review(out, diff_text, t1 - t0, reviewer.model))


if __name__ == "__main__":
    main()
