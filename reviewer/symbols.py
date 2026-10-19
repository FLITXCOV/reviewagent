"""Deterministic symbol index built with Python's `ast` (no embeddings).
Name-based resolution: it is NOT a type-aware resolver (that would need jedi/LSP)."""
from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {"venv", ".venv", "node_modules", "build", "dist", "site-packages", "__pycache__", "vendor"}
MAX_CHUNK_LINES = 60
GENERIC_NAMES = frozenset({
    "get", "set", "run", "read", "write", "add", "update", "close", "open", "main", "start", "stop",
    "send", "load", "save", "items", "keys", "values", "append", "format", "parse", "call", "setup",
    "__init__", "__call__", "__enter__", "__exit__", "__str__", "__repr__",
})
_DEF = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def norm_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def is_test_path(rel: str) -> bool:
    segs = norm_path(rel).lower().split("/")
    base = segs[-1]
    return (any(s in {"test", "tests", "testing"} for s in segs[:-1])
            or base.startswith("test_") or base.endswith("_test.py")
            or base in {"conftest.py", "tests.py", "test.py"})


@dataclass(frozen=True)
class Symbol:
    sid: str                 # "<file>:<qualname>:<start>"
    qualname: str
    name: str
    kind: str                # "function" | "class"
    file: str
    start: int
    end: int
    code: str
    is_test: bool
    callees: tuple[str, ...]


def _called_names(fn: ast.AST) -> tuple[str, ...]:
    names: set[str] = set()
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, _DEF):          # nested defs are separate symbols
            continue
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
        stack.extend(ast.iter_child_nodes(n))
    return tuple(sorted(names))


def _parse(source: str, filename: str = "<unknown>") -> ast.AST:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")          # old code triggers SyntaxWarning (invalid escapes); irrelevant here
        return ast.parse(source, filename=filename)


def enclosing_span(source: str, line: int) -> tuple[int, int] | None:
    """Smallest def/class span containing `line` in `source`, or None."""
    try:
        tree = _parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    best: tuple[int, int] | None = None
    for n in ast.walk(tree):
        if isinstance(n, _DEF):
            s = min([n.lineno] + [d.lineno for d in n.decorator_list])
            e = n.end_lineno or n.lineno
            if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])
                                   or ((e - s) == (best[1] - best[0]) and s > best[0])):
                best = (s, e)
    return best


class SymbolIndex:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.symbols: list[Symbol] = []
        self.by_name: dict[str, list[Symbol]] = {}
        self.by_file: dict[str, list[Symbol]] = {}
        self.callers_of: dict[str, list[Symbol]] = {}
        self.skipped: list[str] = []
        self._build()

    def _build(self) -> None:
        for p in sorted(q for q in self.root.rglob("*.py") if q.is_file()):
            rel = p.relative_to(self.root).as_posix()           # relative: parent dirs of root never matter
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.split("/")[:-1]):
                continue
            try:
                source = p.read_text(encoding="utf-8")
                tree = _parse(source, rel)
            except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError):
                self.skipped.append(rel)
                continue
            self._index_module(rel, source.splitlines(), tree)
        for s in self.symbols:
            self.by_name.setdefault(s.name, []).append(s)
            self.by_file.setdefault(s.file, []).append(s)
            for c in s.callees:
                self.callers_of.setdefault(c, []).append(s)

    def _index_module(self, rel: str, lines: list[str], tree: ast.AST) -> None:
        test_file = is_test_path(rel)

        def visit(node: ast.AST, prefix: str) -> None:
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, _DEF):
                    qn = f"{prefix}{ch.name}"
                    start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                    end = ch.end_lineno or ch.lineno
                    snippet = lines[start - 1:end]
                    if len(snippet) > MAX_CHUNK_LINES:
                        snippet = snippet[:MAX_CHUNK_LINES] + ["# ... truncated ..."]
                    is_cls = isinstance(ch, ast.ClassDef)
                    self.symbols.append(Symbol(
                        sid=f"{rel}:{qn}:{start}", qualname=qn, name=ch.name,
                        kind="class" if is_cls else "function", file=rel, start=start, end=end,
                        code="\n".join(snippet),
                        is_test=test_file or ch.name.startswith("test"),
                        callees=() if is_cls else _called_names(ch)))
                    visit(ch, qn + ".")
                else:
                    visit(ch, prefix)

        visit(tree, "")

    def find_enclosing(self, file: str, line: int) -> Symbol | None:
        best: Symbol | None = None
        for s in self.by_file.get(norm_path(file), []):
            if s.start <= line <= s.end:
                if best is None or (s.end - s.start) < (best.end - best.start) \
                        or ((s.end - s.start) == (best.end - best.start) and s.start > best.start):
                    best = s
        return best

    def retrieve(self, file: str, line: int, top_k: int = 5) -> list[Symbol]:
        """Deterministic context for the symbol enclosing (file, line).
        Priority order: tests[:1], callers[:2], callees[:2], then leftovers in the same order. Capped at top_k."""
        target = self.find_enclosing(file, line)
        if target is None:
            return []
        name = target.name

        tests = sorted((s for s in self.symbols if s.is_test and s.sid != target.sid and
                        (s.name == f"test_{name}" or s.name.startswith(f"test_{name}_") or name in s.callees)),
                       key=lambda s: (s.file, s.start))

        callees: list[Symbol] = []
        for cname in sorted(target.callees):
            defs = [s for s in self.by_name.get(cname, []) if s.sid != target.sid and (target.is_test or not s.is_test)]
            same = sorted((s for s in defs if s.file == target.file), key=lambda s: s.start)
            if same:
                callees.append(same[0])
            elif len(defs) == 1:
                callees.append(defs[0])          # otherwise ambiguous bare name -> skipped on purpose
        callees.sort(key=lambda s: (s.file != target.file, s.file, s.start))

        callers = [s for s in self.callers_of.get(name, []) if s.sid != target.sid and not s.is_test]
        if name in GENERIC_NAMES or len(self.by_name.get(name, [])) > 3:
            callers = [s for s in callers if s.file == target.file]   # unreliable name: same-file only
        callers.sort(key=lambda s: (s.file != target.file, s.file, s.start))

        ordered = tests[:1] + callers[:2] + callees[:2] + tests[1:] + callers[2:] + callees[2:]
        out: list[Symbol] = []
        seen = {target.sid}
        for s in ordered:
            if s.sid not in seen:
                seen.add(s.sid)
                out.append(s)
            if len(out) >= top_k:
                break
        return out
