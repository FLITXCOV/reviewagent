"""python -m eval.build_dataset --bugsinpy data/BugsInPy --repos data/repos --out data/cases
Selects 50 cases (seed 42), clones any missing project repos, writes one JSON per case + data/manifest.json."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from eval.dataset import assign_split, build_case, candidates, save_case, select_cases


def ensure_clone(url: str, dest: Path) -> None:
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", url, str(dest)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bugsinpy", default="data/BugsInPy")
    ap.add_argument("--repos", default="data/repos")
    ap.add_argument("--out", default="data/cases")
    a = ap.parse_args()
    selected = select_cases(candidates(a.bugsinpy))
    splits = assign_split(selected)
    manifest = []
    for ref in selected:
        repo = Path(a.repos) / ref.project
        ensure_clone(ref.github_url, repo)
        case = build_case(ref, repo, a.bugsinpy, splits[ref.case_id])
        if not case["fix_matches_dataset_patch"]:
            raise SystemExit(f"{ref.case_id}: real fix differs from the dataset patch; investigate before continuing")
        save_case(case, a.out)
        manifest.append({k: case[k] for k in ("case_id", "split", "stratum", "file", "buggy_commit", "fixed_commit")})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    (Path(a.out).parent / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"wrote {len(manifest)} cases to {a.out}")


if __name__ == "__main__":
    main()
