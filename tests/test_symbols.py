import os, subprocess, sys, textwrap
from pathlib import Path
from reviewer.symbols import SymbolIndex, enclosing_span, is_test_path


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src), encoding="utf-8")
    return root

REPO = {
    "pkg/pay.py": """\
        def validate_token(t):
            return bool(t)

        def helper():
            return 1

        class Pay:
            def process_payment(self, t):
                if not validate_token(t):
                    raise ValueError
                return helper()
        """,
    "pkg/api.py": """\
        from pkg.pay import Pay
        def handle(req):
            return Pay().process_payment(req)
        """,
    "pkg/latest.py": """\
        def get_latest():
            return 1
        """,
    "tests/test_pay.py": """\
        from pkg.pay import Pay
        def test_process_payment():
            assert Pay().process_payment("x")
        """,
    "pkg/broken.py": "def (:\n",
}


def test_enclosing_is_smallest_span(tmp_path):
    idx = SymbolIndex(write(tmp_path, REPO))
    s = idx.find_enclosing("pkg/pay.py", 10)          # inside the method, not the class
    assert s.qualname == "Pay.process_payment"
    assert idx.find_enclosing("pkg/pay.py", 7).qualname == "Pay"   # the `class` line itself
    assert idx.find_enclosing("pkg/pay.py", 4).qualname == "helper"


def test_retrieve_tests_callers_callees(tmp_path):
    idx = SymbolIndex(write(tmp_path, REPO))
    got = [s.qualname for s in idx.retrieve("pkg/pay.py", 10)]
    assert got[0] == "test_process_payment"             # tests first
    assert "handle" in got                              # caller
    assert "validate_token" in got and "helper" in got  # callees
    assert len(got) <= 5


def test_test_detection_is_not_substring_based(tmp_path):
    idx = SymbolIndex(write(tmp_path, REPO))
    assert idx.by_name["get_latest"][0].is_test is False
    assert is_test_path("tests/x.py") and is_test_path("a/test_x.py") and not is_test_path("a/latest.py")


def test_syntax_error_files_are_skipped_not_fatal(tmp_path):
    idx = SymbolIndex(write(tmp_path, REPO))
    assert "pkg/broken.py" in idx.skipped


def test_repo_under_hidden_parent_dir_still_indexed(tmp_path):
    root = write(tmp_path / ".hidden" / "work", REPO)
    idx = SymbolIndex(root)
    assert idx.find_enclosing("pkg/pay.py", 10) is not None


def test_ambiguous_callee_is_skipped_and_generic_callers_same_file_only(tmp_path):
    files = {
        "a.py": "def go():\n    return dup()\n",
        "b.py": "def dup():\n    return 1\n",
        "c.py": "def dup():\n    return 2\n",
        "d.py": "def get():\n    return 1\n",
        "e.py": "def use():\n    return get()\n",
    }
    idx = SymbolIndex(write(tmp_path, files))
    assert idx.retrieve("a.py", 2) == []               # `dup` defined twice, none in a.py -> ambiguous
    assert [s.qualname for s in idx.retrieve("d.py", 2)] == []   # `get` is generic -> cross-file callers ignored


def test_retrieval_is_deterministic_across_hash_seeds(tmp_path):
    root = write(tmp_path, REPO)
    code = ("import sys; sys.path.insert(0, %r); from reviewer.symbols import SymbolIndex; "
            "print([s.sid for s in SymbolIndex(%r).retrieve('pkg/pay.py', 10)])") % (os.getcwd(), str(root))
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2", "3", "4")}
    assert len(outs) == 1 and "[" in next(iter(outs))


def test_enclosing_span_helper_and_decorators():
    src = "import x\n\n@deco\ndef f():\n    return 1\n\nclass C:\n    def m(self):\n        pass\n"
    assert enclosing_span(src, 5) == (3, 5)            # decorator line is part of the span
    assert enclosing_span(src, 9) == (8, 9)
    assert enclosing_span(src, 1) is None
    assert enclosing_span("def (:", 1) is None
