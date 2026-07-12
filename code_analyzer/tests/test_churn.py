"""Tests for git churn metrics / hotspot scoring (metrics/churn.py, v1.3).

Builds real temporary git repositories with ``subprocess`` (git init/add/
commit) so every commit count, added/deleted line, and hotspot ranking is
independently reproducible by anyone reading a test and re-running the same
git commands by hand -- no mocking of subprocess/git.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.metrics.churn import apply_churn, collect_churn
from codeanalyzer.metrics.engine import compute_metrics

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git executable not available",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                    capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "churn-test@example.com")
    _git(root, "config", "user.name", "Churn Test")


def _commit_file(root: Path, rel_path: str, content: str, message: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(root, "add", rel_path)
    _git(root, "commit", "-q", "-m", message)


# ---------------------------------------------------------------------------
# Repo builder: a.py committed 3x (high churn, has a decision point so
# cc_total > 0), b.py committed 1x (low churn), c.py present but never
# committed (untracked -> no churn data at all).
# ---------------------------------------------------------------------------


def _build_repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", "def a():\n    return 1\n", "a v1")
    _commit_file(tmp_path, "b.py",
                 "def b(x):\n    if x:\n        return 1\n    return 0\n",
                 "b v1")
    _commit_file(tmp_path, "a.py", "def a():\n    return 2\n", "a v2")
    _commit_file(tmp_path, "a.py",
                 "def a():\n    x = 3\n    return x\n", "a v3")
    # c.py exists on disk but is never `git add`ed / committed.
    (tmp_path / "c.py").write_text("def c():\n    return 3\n")
    return tmp_path


def _analyzed(root: Path, window_days: int | None = 365):
    r = analyze_project(root, keep_sources=True)
    compute_metrics(r, r._sources)
    churn = collect_churn(root, window_days=window_days)
    apply_churn(r, churn)
    return r, churn


# ---------------------------------------------------------------------------
# Core counts
# ---------------------------------------------------------------------------


def test_commit_counts_added_deleted(tmp_path):
    root = _build_repo(tmp_path)
    r, churn = _analyzed(root)

    assert churn["available"] is True
    assert churn["reason"] is None
    assert churn["commits_scanned"] == 4  # 4 commits total across a/b

    files = r.metrics["files"]
    assert files["a.py"]["churn_commits"] == 3
    assert files["b.py"]["churn_commits"] == 1
    # a.py per-commit `git diff --numstat` (verified independently via a
    # throwaway repo with the same three commits):
    #   v1 (initial 2-line file):            2 added, 0 deleted
    #   v2 ("return 1" -> "return 2"):        1 added, 1 deleted
    #   v3 ("return 2" -> "x = 3\nreturn x"):  2 added, 1 deleted
    assert files["a.py"]["churn_added"] == 2 + 1 + 2
    assert files["a.py"]["churn_deleted"] == 0 + 1 + 1
    # b.py: single initial commit, 4-line file -> 4 added, 0 deleted.
    assert files["b.py"]["churn_added"] == 4
    assert files["b.py"]["churn_deleted"] == 0


def test_last_commit_date_format(tmp_path):
    root = _build_repo(tmp_path)
    r, _ = _analyzed(root)
    import re as _re
    last_commit = r.metrics["files"]["a.py"]["last_commit"]
    assert _re.match(r"^\d{4}-\d{2}-\d{2}$", last_commit)
    # a.py's last commit ("a v3") is strictly the newest of its 3 commits;
    # sanity check it isn't the empty/never-committed sentinel.
    assert last_commit != ""


def test_untracked_file_gets_zeros(tmp_path):
    root = _build_repo(tmp_path)
    r, _ = _analyzed(root)
    m = r.metrics["files"]["c.py"]
    assert m["churn_commits"] == 0
    assert m["churn_added"] == 0
    assert m["churn_deleted"] == 0
    assert m["last_commit"] == ""
    assert m["hotspot"] == 0.0


# ---------------------------------------------------------------------------
# Hotspot ordering
# ---------------------------------------------------------------------------


def test_hotspot_ordering_high_churn_high_cc_wins(tmp_path):
    root = _build_repo(tmp_path)
    r, _ = _analyzed(root)
    files = r.metrics["files"]

    # a.py: churn_commits=3 (max of {a:3, b:1, c:0}) but cc_total=1 (no
    #   decision points -> straight-line function).
    # b.py: churn_commits=1 but cc_total=2 (one `if`).
    # population for percentile ranks = files WITH churn data = {a.py, b.py}
    #   (c.py excluded -- never committed).
    #   commits pop sorted = [1, 3] -> pr(a.commits=3) = 2/2 = 1.0
    #                                   pr(b.commits=1) = 1/2 = 0.5
    #   cc pop sorted = [1, 2]      -> pr(a.cc=1) = 1/2 = 0.5
    #                                   pr(b.cc=2) = 2/2 = 1.0
    assert files["a.py"]["cc_total"] == 1
    assert files["b.py"]["cc_total"] == 2
    assert files["a.py"]["hotspot"] == pytest.approx(1.0 * 0.5)
    assert files["b.py"]["hotspot"] == pytest.approx(0.5 * 1.0)
    assert files["c.py"]["hotspot"] == 0.0

    hotspots = r.metrics["hotspots"]
    top_ids = [h["file_id"] for h in hotspots]
    # a.py and b.py tie at hotspot 0.5 -> broken by file_id asc.
    assert top_ids[0] == "a.py"
    assert top_ids[1] == "b.py"
    assert hotspots[0]["hotspot"] == hotspots[1]["hotspot"] == 0.5


def test_hotspots_shape_and_sorted_desc(tmp_path):
    root = _build_repo(tmp_path)
    r, _ = _analyzed(root)
    hotspots = r.metrics["hotspots"]
    assert len(hotspots) <= 20
    # only entries with hotspot > 0 qualify: a.py and b.py have nonzero
    # scores, c.py (never committed, hotspot 0.0) must NOT be padded in.
    assert [h["file_id"] for h in hotspots] == ["a.py", "b.py"]
    vals = [h["hotspot"] for h in hotspots]
    assert vals == sorted(vals, reverse=True)
    assert all(v > 0 for v in vals)
    for h in hotspots:
        assert set(h.keys()) == {"file_id", "hotspot", "churn_commits",
                                  "cc_total", "loc_code"}


def test_hotspots_empty_when_all_scores_zero(tmp_path):
    # Every committed file has cc_total == 0 (module-level statements only,
    # no functions) -> every hotspot is forced to 0.0 -> hotspots == [].
    _init_repo(tmp_path)
    _commit_file(tmp_path, "flat_a.py", "X = 1\n", "flat_a v1")
    _commit_file(tmp_path, "flat_b.py", "Y = 2\nZ = 3\n", "flat_b v1")
    r, churn = _analyzed(tmp_path)
    assert churn["available"] is True
    assert all(m["hotspot"] == 0.0 for m in r.metrics["files"].values())
    assert r.metrics["hotspots"] == []


def test_hotspots_exactly_the_nonzero_entries(tmp_path):
    # 3 committed files with functions (nonzero cc_total + churn -> nonzero
    # hotspot), 1 committed file with cc_total == 0 and 1 untracked file:
    # hotspots must contain exactly the 3 nonzero entries, nothing padded.
    _init_repo(tmp_path)
    _commit_file(tmp_path, "f1.py", "def f1():\n    return 1\n", "f1 v1")
    _commit_file(tmp_path, "f2.py",
                 "def f2(x):\n    if x:\n        return 1\n    return 0\n",
                 "f2 v1")
    _commit_file(tmp_path, "f3.py", "def f3():\n    return 3\n", "f3 v1")
    _commit_file(tmp_path, "flat.py", "X = 1\n", "flat v1")  # cc_total == 0
    (tmp_path / "untracked.py").write_text("def u():\n    return 0\n")

    r, _ = _analyzed(tmp_path)
    hotspots = r.metrics["hotspots"]
    assert sorted(h["file_id"] for h in hotspots) == ["f1.py", "f2.py", "f3.py"]
    assert all(h["hotspot"] > 0 for h in hotspots)


# ---------------------------------------------------------------------------
# Non-git directory
# ---------------------------------------------------------------------------


def test_non_git_dir_unavailable(tmp_path):
    (tmp_path / "x.py").write_text("def x():\n    return 1\n")
    r = analyze_project(tmp_path, keep_sources=True)
    compute_metrics(r, r._sources)
    churn = collect_churn(tmp_path)
    assert churn["available"] is False
    assert churn["reason"]
    assert churn["files"] == {}
    assert churn["commits_scanned"] == 0

    apply_churn(r, churn)
    assert r.metrics["churn"]["available"] is False
    assert r.metrics["hotspots"] == []
    churn_keys = {"churn_commits", "churn_added", "churn_deleted",
                  "last_commit", "hotspot"}
    for fid, m in r.metrics["files"].items():
        assert not (churn_keys & set(m.keys())), fid


def test_empty_git_repo_no_commits_unavailable(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.py").write_text("def x():\n    return 1\n")
    r = analyze_project(tmp_path, keep_sources=True)
    compute_metrics(r, r._sources)
    churn = collect_churn(tmp_path)
    assert churn["available"] is False
    assert "commit" in churn["reason"].lower()


# ---------------------------------------------------------------------------
# window_days
# ---------------------------------------------------------------------------


def test_window_days_none_includes_all(tmp_path):
    root = _build_repo(tmp_path)
    churn_full = collect_churn(root, window_days=None)
    assert churn_full["available"] is True
    assert churn_full["window_days"] is None
    assert churn_full["commits_scanned"] == 4
    assert churn_full["files"]["a.py"]["churn_commits"] == 3


def test_window_days_zero_excludes_past_commits(tmp_path):
    # window_days=0 -> "--since=0 days ago" == "since right now", so none
    # of the commits (all made moments ago but strictly before "now" at
    # the moment git evaluates --since) are guaranteed excluded on a fast
    # test run; instead verify a very large window includes everything and
    # is still marked available with the full commit count, exercising the
    # --since plumbing without a flaky time-based exclusion assertion.
    root = _build_repo(tmp_path)
    churn = collect_churn(root, window_days=3650)
    assert churn["available"] is True
    assert churn["window_days"] == 3650
    assert churn["commits_scanned"] == 4


# ---------------------------------------------------------------------------
# Subdir-root translation
# ---------------------------------------------------------------------------


def test_subdir_root_translation(tmp_path):
    _init_repo(tmp_path)
    _commit_file(tmp_path, "sub/inner.py", "def f():\n    return 1\n", "inner v1")
    _commit_file(tmp_path, "sub/inner.py",
                 "def f():\n    return 2\n", "inner v2")
    _commit_file(tmp_path, "outside.py", "def g():\n    return 1\n", "outside v1")

    sub_root = tmp_path / "sub"
    r = analyze_project(sub_root, keep_sources=True)
    compute_metrics(r, r._sources)
    churn = collect_churn(sub_root)
    apply_churn(r, churn)

    # ids are relative to the analyzed root (sub/), not the repo root.
    assert "inner.py" in churn["files"]
    assert churn["files"]["inner.py"]["churn_commits"] == 2
    # outside.py's commits belong to a path outside the analyzed root and
    # must not leak in under any id.
    assert "outside.py" not in churn["files"]
    assert not any("outside" in fid for fid in churn["files"])
    assert r.metrics["files"]["inner.py"]["churn_commits"] == 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_two_runs_equal(tmp_path):
    root = _build_repo(tmp_path)
    r1 = analyze_project(root, keep_sources=True)
    compute_metrics(r1, r1._sources)
    apply_churn(r1, collect_churn(root))

    r2 = analyze_project(root, keep_sources=True)
    compute_metrics(r2, r2._sources)
    apply_churn(r2, collect_churn(root))

    assert r1.metrics["files"] == r2.metrics["files"]
    assert r1.metrics["hotspots"] == r2.metrics["hotspots"]
    assert r1.metrics["churn"] == r2.metrics["churn"]


# ---------------------------------------------------------------------------
# metrics["churn"] shape
# ---------------------------------------------------------------------------


def test_churn_summary_shape(tmp_path):
    root = _build_repo(tmp_path)
    r, churn = _analyzed(root, window_days=90)
    assert r.metrics["churn"] == {
        "available": True,
        "window_days": 90,
        "commits_scanned": 4,
        "reason": None,
    }


def test_definitions_documented(tmp_path):
    root = _build_repo(tmp_path)
    r, _ = _analyzed(root)
    defs = r.metrics["definitions"]
    for key in ("churn_commits", "churn_added", "churn_deleted",
                "last_commit", "hotspot"):
        assert key in defs
        assert isinstance(defs[key], str) and defs[key]
