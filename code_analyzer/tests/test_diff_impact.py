"""Tests for diff-driven change-impact analysis (v1.3 addition).

These build a REAL temporary git repository (``git init`` + two commits) from
the shared ``tests/fixtures/impact_project`` fixture, then exercise
``extract_diff_hunks`` and ``analyze_impact_diff`` against actual ``git diff``
output.

Fixture call graph (see ``tests/test_impact.py``): ``a() -> b() -> c()`` across
``mod_a/mod_b/mod_c.py``; ``importer.py`` import-only depends on ``mod_c``.

Second commit changes made here:
* ``mod_c.py`` -- edit inside the body of ``c`` (line 2);
* ``mod_b.py`` -- insert a brand new function ``extra()`` before ``b``;
* ``README.md`` -- edited (unsupported language, must be skipped);
* ``flow.py`` -- deleted (no new side, must be absent).
"""
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.impact.analyzer import format_impact_text
from codeanalyzer.impact.diff_impact import (
    analyze_impact_diff,
    extract_diff_hunks,
)

FIXTURE = Path(__file__).parent / "fixtures" / "impact_project"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


def _init_commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def build_repo(tmp_path: Path) -> Path:
    """Build a two-commit git repo from the fixture and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for src in sorted(FIXTURE.iterdir()):
        if src.is_file():
            shutil.copy(src, repo / src.name)
    # add an unsupported-language file to the initial commit
    (repo / "README.md").write_text("# project\n\nintro paragraph\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "Tester")
    _init_commit(repo, "initial")

    # -- second commit -------------------------------------------------
    # edit inside the body of c() (line 2)
    (repo / "mod_c.py").write_text(
        "def c(val):\n    result = val + 100\n    return result\n\n\nX = 1\n"
    )
    # insert a NEW function extra() before b() so the hunk starts inside it
    (repo / "mod_b.py").write_text(
        "from mod_c import c\n\n\ndef extra():\n    return c(1)\n"
        "\n\ndef b(n):\n    return c(n)\n"
    )
    # unsupported-language edit
    (repo / "README.md").write_text("# project\n\nCHANGED intro paragraph\n")
    # delete a file (no new side)
    (repo / "flow.py").unlink()
    _init_commit(repo, "second")
    return repo


def _hunks_by_file(hunks):
    by_file = defaultdict(list)
    for h in hunks:
        by_file[h["file"]].append(h)
    return by_file


# ---------------------------------------------------------------------------
# extract_diff_hunks
# ---------------------------------------------------------------------------
def test_extract_diff_hunks_head1(tmp_path):
    repo = build_repo(tmp_path)
    hunks = extract_diff_hunks(repo, "HEAD~1")
    assert isinstance(hunks, list)
    by_file = _hunks_by_file(hunks)

    # mod_c.py: single-line body edit -> analyzed hunk 2-2
    assert by_file["mod_c.py"] == [
        {"file": "mod_c.py", "start_line": 2, "end_line": 2,
         "status": "analyzed", "reason": None}
    ]

    # mod_b.py: new function inserted -> analyzed hunk starting at the def line
    mb = by_file["mod_b.py"]
    assert len(mb) == 1
    assert mb[0]["status"] == "analyzed"
    assert mb[0]["start_line"] == 4

    # README.md: unsupported language -> skipped with reason
    md = by_file["README.md"]
    assert md and all(
        h["status"] == "skipped" and h["reason"] == "unsupported language"
        for h in md
    )

    # deleted file has no new side -> absent entirely
    assert "flow.py" not in by_file

    # deterministic ordering (file, start, end, status)
    keys = [(h["file"], h["start_line"], h["end_line"], h["status"])
            for h in hunks]
    assert keys == sorted(keys)


def test_extract_diff_hunks_worktree(tmp_path):
    repo = build_repo(tmp_path)
    # uncommitted edit to the `return b(5)` line (line 6) of mod_a.py
    p = repo / "mod_a.py"
    p.write_text(p.read_text().replace("return b(5)", "return b(6)"))

    hunks = extract_diff_hunks(repo, "worktree")
    by_file = _hunks_by_file(hunks)
    assert any(
        h["status"] == "analyzed" and h["start_line"] == 6 and h["end_line"] == 6
        for h in by_file["mod_a.py"]
    )
    # without the edit committed, HEAD~1..HEAD must NOT contain mod_a.py
    committed = extract_diff_hunks(repo, "HEAD~1")
    assert all(h["file"] != "mod_a.py" for h in committed)


def test_extract_explicit_range(tmp_path):
    repo = build_repo(tmp_path)
    a = extract_diff_hunks(repo, "HEAD~1..HEAD")
    b = extract_diff_hunks(repo, "HEAD~1")
    assert a == b  # single rev is sugar for <rev>..HEAD


# ---------------------------------------------------------------------------
# analyze_impact_diff
# ---------------------------------------------------------------------------
def test_analyze_impact_diff_origins_and_chain(tmp_path):
    repo = build_repo(tmp_path)
    result = analyze_project(repo)
    imp = analyze_impact_diff(result, repo, "HEAD~1")
    assert "error" not in imp

    origin_ids = {o["symbol_id"] for o in imp["origins"]}
    assert "mod_c.py::c" in origin_ids
    assert "mod_b.py::extra" in origin_ids
    assert imp["origin"] == imp["origins"][0]

    # the a() -> b() -> c() backward chain from the mod_c edit
    smap = {s["symbol_id"]: s
            for af in imp["affected_files"] for s in af["symbols"]}
    assert "mod_b.py::b" in smap
    assert "mod_a.py::a" in smap
    assert smap["mod_b.py::b"]["hops"] == 1
    assert smap["mod_a.py::a"]["hops"] == 2
    # import-only dependent still surfaces at module level
    assert "importer.py::<module>" in smap

    # merged confidence stays certain along the (unambiguous) call chain
    assert smap["mod_b.py::b"]["confidence"] == "certain"

    # stats recomputed over the merged sets
    assert imp["stats"]["files"] == len(imp["affected_files"])
    assert imp["stats"]["symbols"] == sum(
        len(af["symbols"]) for af in imp["affected_files"])
    assert isinstance(imp["stats"]["max_hops_reached"], bool)


def test_analyze_impact_diff_hunk_statuses(tmp_path):
    repo = build_repo(tmp_path)
    result = analyze_project(repo)
    imp = analyze_impact_diff(result, repo, "HEAD~1")

    statuses = {(h["file"], h["status"]) for h in imp["hunks"]}
    assert ("mod_c.py", "analyzed") in statuses
    assert ("mod_b.py", "analyzed") in statuses
    assert ("README.md", "skipped") in statuses
    # every skipped hunk carries a reason; analyzed carry none
    for h in imp["hunks"]:
        if h["status"] == "skipped":
            assert h["reason"]
        else:
            assert h["reason"] is None


def test_analyze_impact_diff_query_shape(tmp_path):
    repo = build_repo(tmp_path)
    result = analyze_project(repo)
    imp = analyze_impact_diff(result, repo, "HEAD~1", max_depth=7)
    q = imp["query"]
    assert q == {"file": None, "start_line": None, "end_line": None,
                 "depth": 7, "diff": "HEAD~1"}
    # contract keys present alongside the base ImpactResult shape
    assert {"origins", "origin", "hunks", "affected_files", "sliced_vars",
            "unresolved_boundaries", "stats", "query"} <= set(imp.keys())


def test_analyze_impact_diff_deterministic(tmp_path):
    repo = build_repo(tmp_path)
    result = analyze_project(repo)
    a = analyze_impact_diff(result, repo, "HEAD~1")
    b = analyze_impact_diff(result, repo, "HEAD~1")
    assert a == b
    # a fresh analysis of the same tree yields an identical merged result
    fresh = analyze_project(repo)
    c = analyze_impact_diff(fresh, repo, "HEAD~1")
    assert a == c


def test_analyze_impact_diff_text_rendering(tmp_path):
    repo = build_repo(tmp_path)
    result = analyze_project(repo)
    imp = analyze_impact_diff(result, repo, "HEAD~1")
    txt = format_impact_text(imp)
    assert isinstance(txt, str) and txt
    assert "mod_c.py::c" in txt          # origin listed
    assert "mod_b.py::extra" in txt      # second origin listed
    assert "mod_a.py::a" in txt          # affected chain rendered
    assert "README.md" in txt            # hunk table shows skipped file
    assert "HEAD~1" in txt               # diff spec in header


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------
def test_not_a_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "mod_c.py").write_text("def c(val):\n    return val\n")

    res = extract_diff_hunks(plain, "worktree")
    assert isinstance(res, dict) and "error" in res

    # analyze_impact_diff passes the git error straight through
    result = analyze_project(plain)
    imp = analyze_impact_diff(result, plain, "worktree")
    assert "error" in imp


def test_no_analyzable_hunks(tmp_path):
    repo = tmp_path / "mdrepo"
    repo.mkdir()
    (repo / "mod_c.py").write_text("def c(val):\n    return val\n")
    (repo / "README.md").write_text("first\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "Tester")
    _init_commit(repo, "initial")
    # second commit only touches the unsupported .md file
    (repo / "README.md").write_text("second\n")
    _init_commit(repo, "docs")

    result = analyze_project(repo)
    imp = analyze_impact_diff(result, repo, "HEAD~1")
    assert "error" in imp
    assert "analyzable" in imp["error"]


def test_extract_no_changes_returns_empty(tmp_path):
    repo = build_repo(tmp_path)
    # HEAD..HEAD -> no diff at all
    hunks = extract_diff_hunks(repo, "HEAD..HEAD")
    assert hunks == []
    # and analyze_impact_diff on an empty diff is an error, not a crash
    result = analyze_project(repo)
    imp = analyze_impact_diff(result, repo, "HEAD..HEAD")
    assert "error" in imp
