"""Builds eval cases from BugsInPy (https://github.com/soarsmu/BugsInPy).
Verified layout: projects/<proj>/project.info (github_url), projects/<proj>/bugs/<id>/bug.info
(buggy_commit_id, fixed_commit_id) and bug_patch.txt (git diff buggy->fixed; may include test files)."""
from __future__ import annotations

import json
import random
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from eval.scoring import GroundTruth
from reviewer.diffparse import changed_new_spans, parse_unified_diff
from reviewer.symbols import enclosing_span, is_test_path

DEFAULT_PROJECTS = ["scrapy", "thefuck", "luigi", "black", "fastapi", "tqdm", "sanic", "httpie", "PySnooper"]


def parse_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r'^\s*([A-Za-z_]+)\s*=\s*"?(.*?)"?\s*$', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def split_patch_by_file(patch_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r"(?m)^(?=diff --git )", patch_text):
        m = re.match(r"diff --git a/(.*?) b/(.*)", part)
        if m:
            out[m.group(2).strip()] = part
    return out


@dataclass(frozen=True)
class BugRef:
    project: str
    bug_id: int
    github_url: str
    buggy: str
    fixed: str
    file: str
    n_hunks: int
    n_changed: int

    @property
    def case_id(self) -> str:
        return f"{self.project}-{self.bug_id}"


def candidates(bugsinpy_root: str | Path, projects: list[str] = DEFAULT_PROJECTS,
               max_hunks: int = 3, max_changed: int = 60) -> list[BugRef]:
    """Bugs whose fix touches exactly ONE non-test .py file, with a small, reviewable change."""
    root = Path(bugsinpy_root) / "projects"
    refs: list[BugRef] = []
    for proj in sorted(projects):
        pinfo = parse_kv(root / proj / "project.info")
        bdir = root / proj / "bugs"
        for bid in sorted((int(p.name) for p in bdir.iterdir() if p.name.isdigit())):
            info = parse_kv(bdir / str(bid) / "bug.info")
            patch = (bdir / str(bid) / "bug_patch.txt").read_text(encoding="utf-8", errors="ignore")
            src = {f: t for f, t in split_patch_by_file(patch).items() if f.endswith(".py") and not is_test_path(f)}
            if len(src) != 1:
                continue
            (path, text), = src.items()
            fds = parse_unified_diff(text)
            if len(fds) != 1:
                continue
            n_hunks = len(fds[0].hunks)
            n_changed = sum(l.kind != " " for h in fds[0].hunks for l in h.lines)
            if 0 < n_hunks <= max_hunks and 0 < n_changed <= max_changed:
                refs.append(BugRef(proj, bid, pinfo["github_url"], info["buggy_commit_id"],
                                   info["fixed_commit_id"], path, n_hunks, n_changed))
    return refs


def select_cases(cands: list[BugRef], per_project_cap: int = 8, target: int = 50, seed: int = 42) -> list[BugRef]:
    rng = random.Random(seed)
    pool: list[BugRef] = []
    for proj in sorted({c.project for c in cands}):
        group = sorted((c for c in cands if c.project == proj), key=lambda c: c.bug_id)
        pool += sorted(rng.sample(group, min(per_project_cap, len(group))), key=lambda c: c.bug_id)
    if len(pool) > target:
        pool = sorted(rng.sample(pool, target), key=lambda c: (c.project, c.bug_id))
    return sorted(pool, key=lambda c: (c.project, c.bug_id))


def assign_split(selected: list[BugRef]) -> dict[str, str]:
    """Every 5th case (sorted order) is DEV (tune prompts on it); the rest are TEST (report only these)."""
    return {c.case_id: ("dev" if i % 5 == 0 else "test") for i, c in enumerate(selected)}


def change_lines(diff_text: str) -> list[str]:
    """The +/- content lines of a diff (headers, hunk markers, context and `index` lines excluded)."""
    return [l.rstrip() for l in diff_text.splitlines()
            if l[:1] in "+-" and not l.startswith(("+++ ", "--- "))]


def git(repo: str | Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def checkout(repo: str | Path, commit: str) -> None:
    git(repo, "checkout", "--quiet", "--force", commit)


def build_case(ref: BugRef, repo_dir: str | Path, bugsinpy_root: str | Path, split: str = "test") -> dict:
    """Needs a full clone of ref.github_url at repo_dir (git clone <url> <dir>)."""
    reversed_diff = git(repo_dir, "diff", "--no-color", "-U3", ref.fixed, ref.buggy, "--", ref.file)   # old=fixed,new=buggy
    forward_diff = git(repo_dir, "diff", "--no-color", "-U3", ref.buggy, ref.fixed, "--", ref.file)    # = the real fix
    dataset_patch = split_patch_by_file((Path(bugsinpy_root) / "projects" / ref.project / "bugs" /
                                         str(ref.bug_id) / "bug_patch.txt").read_text(errors="ignore")).get(ref.file, "")
    fds = parse_unified_diff(reversed_diff)
    if len(fds) != 1:
        raise ValueError(f"{ref.case_id}: expected exactly one file in reversed diff, got {len(fds)}")
    fd = fds[0]
    spans = changed_new_spans(fd)
    buggy_src = git(repo_dir, "show", f"{ref.buggy}:{ref.file}")
    enclosing = [e for e in (enclosing_span(buggy_src, s) for s, _ in spans) if e]
    has_added = any(l.kind == "+" for h in fd.hunks for l in h.lines)
    return {
        "case_id": ref.case_id, "project": ref.project, "bug_id": ref.bug_id, "split": split,
        "file": ref.file, "buggy_commit": ref.buggy, "fixed_commit": ref.fixed,
        "buggy_diff": reversed_diff, "clean_diff": forward_diff, "fix_patch": forward_diff,
        "spans": spans, "enclosing": sorted(set(enclosing)),
        "stratum": "modification" if has_added else "deletion_only",
        "fix_matches_dataset_patch": change_lines(forward_diff) == change_lines(dataset_patch),   # same +/- lines
    }


def ground_truth(case: dict) -> GroundTruth:
    return GroundTruth(case["case_id"], case["file"], tuple(map(tuple, case["spans"])),
                       tuple(map(tuple, case["enclosing"])), case["stratum"], case["fix_patch"])


def save_case(case: dict, out_dir: str | Path) -> Path:
    p = Path(out_dir) / f"{case['case_id']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(case, indent=1), encoding="utf-8")
    return p
