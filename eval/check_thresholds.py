"""CI gate. Exit code 1 if a summary violates thresholds.json.
thresholds.json example: {"recall_lenient_min": 0.30, "fp_rate_clean_max": 0.40, "failed_nodes_max": 0}"""
from __future__ import annotations

import json
import sys


def check(summary: dict, thresholds: dict) -> list[str]:
    problems: list[str] = []
    r = summary["recall_lenient"]
    if "recall_lenient_min" in thresholds and r["n"] and r["k"] / r["n"] < thresholds["recall_lenient_min"]:
        problems.append(f"recall_lenient {r['k']}/{r['n']} below {thresholds['recall_lenient_min']}")
    fp = summary["fp_rate_clean"]
    if "fp_rate_clean_max" in thresholds and fp["n"] and fp["k"] / fp["n"] > thresholds["fp_rate_clean_max"]:
        problems.append(f"fp_rate_clean {fp['k']}/{fp['n']} above {thresholds['fp_rate_clean_max']}")
    if "failed_nodes_max" in thresholds and summary["failed_nodes"] > thresholds["failed_nodes_max"]:
        problems.append(f"failed_nodes {summary['failed_nodes']} above {thresholds['failed_nodes_max']}")
    return problems


def main(argv: list[str]) -> int:
    result = json.load(open(argv[1], encoding="utf-8"))
    problems = check(result["summary"], json.load(open(argv[2], encoding="utf-8")))
    for p in problems:
        print("THRESHOLD VIOLATION:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
