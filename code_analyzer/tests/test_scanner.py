from pathlib import Path

from codeanalyzer.core.scanner import (
    _is_test_file,
    default_excludes,
    scan,
)


def test_scan_collects_expected_files(sample_root):
    files, diags = scan(sample_root, [], [], respect_gitignore=True)
    ids = [f.id for f in files]
    # deterministic sorted order
    assert ids == sorted(ids)
    assert "py/models.py" in ids
    assert "src/app/main.ts" in ids
    assert "src/legacy.js" in ids
    assert "c/util.h" in ids
    assert "cpp/shape.cpp" in ids
    assert "java/com/ex/Main.java" in ids
    # minified file excluded by default glob
    assert "bundle.min.js" not in ids
    # broken file still collected (isolation happens at parse time)
    assert "broken/broken.py" in ids


def test_language_detection(sample_root):
    files, _ = scan(sample_root, [], [], respect_gitignore=True)
    byid = {f.id: f for f in files}
    assert byid["py/models.py"].language.value == "python"
    assert byid["src/app/main.ts"].language.value == "typescript"
    assert byid["src/legacy.js"].language.value == "javascript"
    assert byid["c/main.c"].language.value == "c"
    assert byid["cpp/shape.cpp"].language.value == "cpp"
    assert byid["java/com/ex/Main.java"].language.value == "java"


def test_includes_filter(sample_root):
    files, _ = scan(sample_root, ["*.py"], [], respect_gitignore=True)
    assert files
    assert all(f.id.endswith(".py") for f in files)


def test_user_exclude_dir(sample_root):
    files, _ = scan(sample_root, [], ["cpp"], respect_gitignore=True)
    assert not any(f.id.startswith("cpp/") for f in files)


def test_is_test_detection():
    assert _is_test_file("tests/test_x.py")
    assert _is_test_file("pkg/test_foo.py")
    assert _is_test_file("pkg/foo_test.py")
    assert _is_test_file("a/foo.spec.ts")
    assert _is_test_file("a/foo.test.js")
    assert _is_test_file("test/helper.c")
    assert not _is_test_file("pkg/models.py")
    assert not _is_test_file("src/app/main.ts")


def test_default_excludes_recorded():
    ex = default_excludes()
    assert "node_modules" in ex
    assert "*.min.js" in ex
    assert ex == sorted(ex)


def test_size_skip_and_encoding(tmp_path: Path):
    big = tmp_path / "big.py"
    big.write_bytes(b"x = 1\n" + b"# pad\n" * (5 * 1024 * 1024 // 6 + 10))
    latin = tmp_path / "lat.py"
    latin.write_bytes("x = 'caf\xe9'\n".encode("latin-1"))
    files, diags = scan(tmp_path, [], [], respect_gitignore=False)
    ids = [f.id for f in files]
    assert "big.py" not in ids
    assert "lat.py" in ids
    kinds = {(d.file, d.kind) for d in diags}
    assert ("big.py", "skipped_size") in kinds
    assert ("lat.py", "encoding_fallback") in kinds


def test_gitignore_respected(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("ignored.py\nskip/\n")
    (tmp_path / "keep.py").write_text("a = 1\n")
    (tmp_path / "ignored.py").write_text("b = 2\n")
    skip = tmp_path / "skip"
    skip.mkdir()
    (skip / "inner.py").write_text("c = 3\n")

    files, _ = scan(tmp_path, [], [], respect_gitignore=True)
    ids = {f.id for f in files}
    assert "keep.py" in ids
    assert "ignored.py" not in ids
    assert "skip/inner.py" not in ids

    files2, _ = scan(tmp_path, [], [], respect_gitignore=False)
    ids2 = {f.id for f in files2}
    assert "ignored.py" in ids2
