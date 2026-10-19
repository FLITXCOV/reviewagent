"""Unified-diff parser with explicit NEW-SIDE line numbers on every line."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")

LOCKFILES = {"poetry.lock", "Pipfile.lock", "package-lock.json", "yarn.lock", "uv.lock"}
SKIP_DIRS = {"vendor", "node_modules", "dist", "build", "venv", ".venv", "site-packages", "__pycache__"}


@dataclass(frozen=True)
class DiffLine:
    kind: str            # " " context, "+" added, "-" removed
    text: str
    old_no: int | None   # None for "+"
    new_no: int | None   # None for "-"
    anchor: int          # new-side line this line sits at; for "-" = the next new-side line


@dataclass
class Hunk:
    file: str
    old_start: int
    new_start: int
    section: str
    lines: list[DiffLine]


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    _saw_minus: bool = False


def _strip_ab(p: str) -> str:
    p = p.split("\t")[0].strip()
    return p[2:] if p.startswith(("a/", "b/")) else p


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    rem_old = rem_new = old_no = new_no = 0
    from_git_header = False

    for raw in text.splitlines():
        # ---- inside a hunk body: counts decide where the hunk ends ----
        if hunk is not None and (rem_old > 0 or rem_new > 0):
            if raw.startswith("\\"):          # "\ No newline at end of file"
                continue
            kind, body = (" ", "") if raw == "" else (raw[0], raw[1:])
            if kind == " ":
                hunk.lines.append(DiffLine(" ", body, old_no, new_no, new_no))
                old_no += 1; new_no += 1; rem_old -= 1; rem_new -= 1
                continue
            if kind == "+":
                hunk.lines.append(DiffLine("+", body, None, new_no, new_no))
                new_no += 1; rem_new -= 1
                continue
            if kind == "-":
                hunk.lines.append(DiffLine("-", body, old_no, None, new_no))
                old_no += 1; rem_old -= 1
                continue
            hunk, rem_old, rem_new = None, 0, 0   # malformed body: fall through as a header line
        if raw.startswith("\\"):
            continue

        # ---- headers ----
        m = DIFF_GIT_RE.match(raw)
        if m:
            cur = FileDiff(path=m.group(2), old_path=m.group(1))
            files.append(cur)
            hunk, from_git_header = None, True
            continue
        if raw.startswith("--- "):
            if cur is None or cur.hunks or cur._saw_minus:
                cur = FileDiff(path="")
                files.append(cur)
                from_git_header = False
            cur._saw_minus = True
            old = raw[4:].split("\t")[0].strip()
            if old == "/dev/null":
                cur.is_new = True
            elif not from_git_header:
                cur.old_path = _strip_ab(old)
            continue
        if raw.startswith("+++ ") and cur is not None:
            new = raw[4:].split("\t")[0].strip()
            if new == "/dev/null":
                cur.is_deleted = True
                if not cur.path:
                    cur.path = cur.old_path or ""
            elif not from_git_header:
                cur.path = _strip_ab(new)
            continue
        if cur is not None and (raw.startswith("Binary files ") or raw.startswith("GIT binary patch")):
            cur.is_binary = True
            continue
        m = HUNK_RE.match(raw)
        if m and cur is not None:
            os_, oc, ns, nc = int(m[1]), m[2], int(m[3]), m[4]
            oc = 1 if oc is None else int(oc)
            nc = 1 if nc is None else int(nc)
            old_no = os_ if oc > 0 else os_ + 1
            new_no = ns if nc > 0 else ns + 1
            rem_old, rem_new = oc, nc
            hunk = Hunk(file=cur.path, old_start=os_, new_start=ns, section=m[5].strip(), lines=[])
            cur.hunks.append(hunk)
            if rem_old == 0 and rem_new == 0:
                hunk = None
    return files


def should_review_path(path: str, skip_tests: bool = False) -> bool:
    segs = path.replace("\\", "/").split("/")
    base = segs[-1]
    if not base.endswith(".py") or base in LOCKFILES or base.endswith("_pb2.py"):
        return False
    if any(s in SKIP_DIRS for s in segs[:-1]):
        return False
    if skip_tests:
        from reviewer.symbols import is_test_path
        return not is_test_path(path)
    return True


def reviewable_files(files: list[FileDiff], skip_tests: bool = False) -> list[FileDiff]:
    return [f for f in files
            if not f.is_deleted and not f.is_binary and f.hunks and should_review_path(f.path, skip_tests)]


def split_hunk(h: Hunk, max_lines: int = 120, overlap: int = 20) -> list[Hunk]:
    """Split an oversized hunk into overlapping windows; every window keeps correct new-side numbers."""
    if max_lines <= overlap:
        raise ValueError("max_lines must be greater than overlap")
    if len(h.lines) <= max_lines:
        return [h]
    out: list[Hunk] = []
    step, i = max_lines - overlap, 0
    while True:
        win = h.lines[i:i + max_lines]
        old_first = next((l.old_no for l in win if l.old_no is not None), h.old_start)
        out.append(Hunk(file=h.file, old_start=old_first, new_start=win[0].anchor,
                        section=h.section, lines=win))
        if i + max_lines >= len(h.lines):
            break
        i += step
    return out


def changed_new_spans(fd: FileDiff) -> list[tuple[int, int]]:
    """New-side line spans of each contiguous change block.
    Block with added lines -> (first_added, last_added).
    Pure-deletion block   -> (anchor-1, anchor): the two lines around the deletion point."""
    spans: list[tuple[int, int]] = []
    for h in fd.hunks:
        block: list[DiffLine] = []
        for l in h.lines + [DiffLine(" ", "", None, None, 0)]:
            if l.kind != " ":
                block.append(l)
                continue
            if block:
                added = [b.new_no for b in block if b.kind == "+"]
                if added:
                    spans.append((min(added), max(added)))
                else:
                    a = block[0].anchor
                    spans.append((max(1, a - 1), a))
                block = []
    return spans


def render_hunk(h: Hunk) -> str:
    """Text shown to the LLM. Left column = NEW-file line number (blank for removed lines)."""
    out = [f"### {h.file}  (new-file line numbers; first line = {h.new_start})"]
    for l in h.lines:
        num = f"{l.new_no:>6}" if l.new_no is not None else " " * 6
        out.append(f"{num} {l.kind} {l.text}")
    return "\n".join(out)
