import pytest
from reviewer.diffparse import (changed_new_spans, parse_unified_diff, render_hunk, reviewable_files,
                                should_review_path, split_hunk)

MOD = """diff --git a/pkg/a.py b/pkg/a.py
index 111..222 100644
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -10,5 +10,5 @@ def f():
 ctx1
 ctx2
-old line
+new line
 ctx3
 ctx4
"""

TWO_FILES = """diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
diff --git a/b.py b/b.py
index 3..4 100644
--- a/b.py
+++ b/b.py
@@ -5,2 +5,3 @@
 keep
+added
 keep2
"""


def test_line_numbers_modification():
    (fd,) = parse_unified_diff(MOD)
    assert fd.path == "pkg/a.py" and len(fd.hunks) == 1
    nums = [(l.kind, l.old_no, l.new_no) for l in fd.hunks[0].lines]
    assert nums == [(" ", 10, 10), (" ", 11, 11), ("-", 12, None), ("+", None, 12), (" ", 13, 13), (" ", 14, 14)]
    assert changed_new_spans(fd) == [(12, 12)]


def test_pure_addition_hunk_and_span():
    d = """diff --git a/pkg/add.py b/pkg/add.py
--- a/pkg/add.py
+++ b/pkg/add.py
@@ -10,3 +10,5 @@ def f():
 ctx1
 ctx2
+new1
+new2
 ctx3
"""
    (fd,) = parse_unified_diff(d)
    assert fd.path == "pkg/add.py" and len(fd.hunks) == 1
    nums = [(l.kind, l.old_no, l.new_no) for l in fd.hunks[0].lines]
    assert nums == [(" ", 10, 10), (" ", 11, 11), ("+", None, 12), ("+", None, 13), (" ", 12, 14)]
    assert changed_new_spans(fd) == [(12, 13)]


def test_two_files_headers_do_not_leak():
    a, b = parse_unified_diff(TWO_FILES)
    assert [l.text for l in a.hunks[0].lines] == ["x = 1", "x = 2", "y = 3"]
    assert b.path == "b.py" and [l.kind for l in b.hunks[0].lines] == [" ", "+", " "]
    assert changed_new_spans(b) == [(6, 6)]


def test_deleted_new_binary_and_nonpy_filtered():
    d = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a
+b
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-x
+y
"""
    files = parse_unified_diff(d)
    assert [f.path for f in files] == ["gone.py", "new.py", "img.png", "README.md"]
    assert files[0].is_deleted and files[1].is_new and files[2].is_binary
    assert [f.path for f in reviewable_files(files)] == ["new.py"]
    assert [l.new_no for l in files[1].hunks[0].lines] == [1, 2]


def test_pure_deletion_hunk_and_span():
    d = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -4,5 +4,3 @@ def f(x):
 before
-    if x is None:
-        return
 after
 after2
"""
    (fd,) = parse_unified_diff(d)
    ls = fd.hunks[0].lines
    assert [(l.kind, l.new_no, l.anchor) for l in ls] == [(" ", 4, 4), ("-", None, 5), ("-", None, 5), (" ", 5, 5), (" ", 6, 6)]
    assert changed_new_spans(fd) == [(4, 5)]


def test_zero_count_new_side_and_no_newline_marker():
    d = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -3,2 +2,0 @@
-gone1
-gone2
\\ No newline at end of file
"""
    (fd,) = parse_unified_diff(d)
    assert [(l.kind, l.anchor) for l in fd.hunks[0].lines] == [("-", 3), ("-", 3)]


def test_blank_context_line_without_leading_space():
    d = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n"
    (fd,) = parse_unified_diff(d)
    assert [l.kind for l in fd.hunks[0].lines] == [" ", " ", "-", "+"]


def test_plain_unified_diff_without_git_header():
    d = "--- a/x.py\t2020\n+++ b/x.py\t2020\n@@ -1 +1 @@\n-a\n+b\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-c\n+d\n"
    files = parse_unified_diff(d)
    assert [f.path for f in files] == ["x.py", "y.py"]


def test_should_review_path_uses_segments_not_substrings():
    assert should_review_path("pkg/latest.py") and should_review_path("src/builder.py")
    assert not should_review_path("vendor/x.py") and not should_review_path("a/node_modules/x.py")
    assert not should_review_path("poetry.lock") and not should_review_path("x_pb2.py")
    assert not should_review_path("tests/test_a.py", skip_tests=True)
    assert should_review_path("pkg/latest.py", skip_tests=True)


def _big_hunk(n):
    body = "\n".join(f"+line{i}" for i in range(n))
    d = f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,{n} @@\n{body}\n"
    return parse_unified_diff(d)[0].hunks[0]


def test_split_hunk_numbering_and_coverage():
    h = _big_hunk(300)
    parts = split_hunk(h, max_lines=120, overlap=20)
    assert len(parts) > 1
    assert parts[0].new_start == 1 and parts[1].new_start == 101
    covered = {l.new_no for p in parts for l in p.lines}
    assert covered == set(range(1, 301))
    for p in parts:
        assert len(p.lines) <= 120 and p.new_start == p.lines[0].new_no


def test_split_hunk_small_unchanged_and_bad_args():
    h = _big_hunk(10)
    assert split_hunk(h) == [h]
    with pytest.raises(ValueError):
        split_hunk(h, max_lines=20, overlap=20)


def test_render_hunk_shows_new_numbers_only():
    (fd,) = parse_unified_diff(MOD)
    text = render_hunk(fd.hunks[0])
    assert "    12 + new line" in text and "       - old line" in text
