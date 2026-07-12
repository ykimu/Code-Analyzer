"""Git churn metrics and hotspot scoring (v1.3 additions, SCHEMA_VERSION 1.2).

``collect_churn(root, window_days)`` shells out to ``git log --numstat`` and
returns a plain, JSON-safe dict describing per-file commit activity.
``apply_churn(result, churn)`` merges that dict into an already-computed
``AnalysisResult.metrics`` (i.e. it must run *after*
``metrics.engine.compute_metrics``, which is what populates
``metrics["files"][fid]["cc_total"]`` -- the other half of the hotspot
formula).

Both functions are pure with respect to the filesystem beyond the read-only
``git log`` subprocess call; ``apply_churn`` is the only one that mutates
its argument (``result.metrics``, in place), matching the rest of the
metrics engine's contract.

See ``core/model.py``'s "v1.3 additions" docstring block for the binding
shape contract this module implements:

    metrics["files"][fid] += {"churn_commits", "churn_added",
                               "churn_deleted", "last_commit", "hotspot"}
    metrics["churn"] = {"available", "window_days", "commits_scanned",
                         "reason"}
    metrics["hotspots"] = top-20 [{"file_id", "hotspot", "churn_commits",
                                    "cc_total", "loc_code"}]

Design decisions (documented, not just asserted by tests):

* **Availability.** ``collect_churn`` returns ``available: False`` for three
  distinct causes, each with a concise ``reason`` string: the ``git``
  executable is missing, ``root`` is not inside a git work tree, or the
  repository genuinely has *no commits yet* (``git rev-parse --verify
  HEAD`` fails). A repository that has commits but none within
  ``window_days`` is a *different* case -- that is a normal, available
  result with ``commits_scanned == 0`` and an empty ``files`` map, because
  the repository itself is not empty, the window just excludes everything.
* **Window.** ``--since="<window_days> days ago"`` is passed to ``git log``
  when ``window_days is not None``; ``window_days is None`` means "full
  history", so no ``--since`` flag is added at all.
* **Subdirectory roots.** ``root`` (the analyzed root) need not be the repo
  root -- ``git log`` always reports paths relative to the *repo* root. We
  read ``git rev-parse --show-prefix`` once (the analyzed root's path
  relative to the repo root, POSIX, trailing slash or ``""``) and strip it
  from every numstat path to translate into analyzed-root-relative file
  ids. Paths that don't fall under that prefix (commits touching files
  outside the analyzed subtree) are skipped entirely -- they cannot map to
  any file id compute_metrics would have produced.
* **Parsing.** ``--format=%H%x09%ct`` emits one header line per commit
  (``<40-hex-hash>TAB<unix-committer-timestamp>``); ``--numstat`` lines
  have three tab-separated fields (``added``, ``deleted``, ``path``).
  Lines are told apart structurally (a regex-verified 40-hex first field
  with exactly one tab vs. exactly two tabs) rather than by relying on
  blank-line placement, which is more robust to git version differences.
  Binary files report ``added``/``deleted`` as ``"-"``; those numstat
  entries still count toward ``churn_commits`` (the commit *did* touch the
  file) but contribute 0 lines.
* **last_commit** is the max committer timestamp per file, rendered as a
  UTC ISO date (``YYYY-MM-DD``) -- UTC (not local time) so results are
  identical regardless of the machine running the analysis.
* **hotspot / percentile_rank.** ``percentile_rank(v) = |{x in pop : x <=
  v}| / n`` (a standard "fraction at or below" percentile), where the
  population ``pop`` is the ``churn_commits`` (resp. ``cc_total``) values
  of exactly the analyzed files that have git churn data (i.e. appear in
  ``churn["files"]``) -- *not* every scanned file, and not weighted by
  git's own commit graph. A file with no churn data (never committed, e.g.
  untracked/new) is excluded from the population entirely, but still gets
  a ``hotspot`` of ``0.0`` in its own metrics row (forced by the
  ``churn_commits == 0`` guard below, so it never needs to consult the
  population). ``hotspot = round(percentile_rank(churn_commits) *
  percentile_rank(cc_total), 4)``, forced to ``0.0`` whenever
  ``churn_commits == 0`` or ``cc_total == 0`` (a file that has never
  changed, or has no complexity to speak of, cannot be a hotspot) -- this
  guard is evaluated before consulting the percentile population, so it
  also correctly zeroes out files with churn data but zero cc_total.
* **Per-file key presence.** When ``churn["available"]`` is true, *every*
  row in ``metrics["files"]`` gains all five keys -- files with no git
  history of their own get ``churn_commits/added/deleted = 0``,
  ``last_commit = ""``, ``hotspot = 0.0``. When unavailable, *no* row gets
  any of the five keys (contract: "only when the project is a git repo").
* **Hotspots list.** Only entries with ``hotspot > 0`` qualify (updated
  contract: padding the top-20 with 0.0 entries would surface meaningless
  "top hotspots" in CLI/UI summaries on repos where few files have churn,
  e.g. shallow clones or a window with zero scanned commits), so the list
  may be shorter than 20 or empty. Sorted by hotspot desc then file_id
  asc for determinism, truncated to 20.
"""
from __future__ import annotations

import re
import subprocess
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeanalyzer.core.model import AnalysisResult

_HASH_RE = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess | None:
    """Run ``git -C root <args>``; return None if the git executable itself
    is missing (a distinct, higher-priority unavailability cause)."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None


def _unavailable(window_days: int | None, reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "files": {},
        "commits_scanned": 0,
        "window_days": window_days,
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def collect_churn(root: Path, window_days: int | None = 365) -> dict[str, Any]:
    """Collect per-file git churn stats under ``root`` for the last
    ``window_days`` days (``None`` = full history). See the module
    docstring for the exact availability/parsing/translation rules."""
    root = Path(root)

    probe = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if probe is None:
        return _unavailable(window_days, "git executable not found")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return _unavailable(window_days, "not a git repository")

    head = _run_git(root, ["rev-parse", "--verify", "HEAD"])
    if head is None or head.returncode != 0:
        return _unavailable(window_days, "repository has no commits")

    prefix_proc = _run_git(root, ["rev-parse", "--show-prefix"])
    prefix = prefix_proc.stdout.strip() if prefix_proc and prefix_proc.returncode == 0 else ""

    cmd = ["log", "--numstat", "--format=%H%x09%ct", "--no-renames"]
    if window_days is not None:
        cmd.append(f"--since={window_days} days ago")
    log_proc = _run_git(root, cmd)
    if log_proc is None:
        return _unavailable(window_days, "git executable not found")
    if log_proc.returncode != 0:
        stderr = (log_proc.stderr or "").strip().splitlines()
        detail = stderr[0] if stderr else "unknown error"
        return _unavailable(window_days, f"git log failed: {detail}")

    commits_scanned, files_acc = _parse_numstat(log_proc.stdout, prefix)

    files: dict[str, dict[str, Any]] = {}
    for fid, entry in files_acc.items():
        last_ts = entry["last_commit_ts"]
        files[fid] = {
            "churn_commits": entry["churn_commits"],
            "churn_added": entry["churn_added"],
            "churn_deleted": entry["churn_deleted"],
            "last_commit": (
                datetime.fromtimestamp(last_ts, tz=timezone.utc).date().isoformat()
                if last_ts is not None else ""
            ),
        }

    return {
        "available": True,
        "reason": None,
        "window_days": window_days,
        "commits_scanned": commits_scanned,
        "files": files,
    }


def _parse_numstat(output: str, prefix: str) -> tuple[int, dict[str, dict[str, Any]]]:
    commits_scanned = 0
    seen_hashes: set[str] = set()
    files_acc: dict[str, dict[str, Any]] = {}

    current_ts: int | None = None

    for line in output.split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 2 and _HASH_RE.match(parts[0]):
            commit_hash, ts_str = parts
            try:
                current_ts = int(ts_str)
            except ValueError:
                current_ts = None
            if commit_hash not in seen_hashes:
                seen_hashes.add(commit_hash)
                commits_scanned += 1
            continue
        if len(parts) == 3 and current_ts is not None:
            added_s, deleted_s, path = parts
            if prefix and not path.startswith(prefix):
                continue
            file_id = path[len(prefix):] if prefix else path
            if not file_id:
                continue
            added = 0 if added_s == "-" else int(added_s)
            deleted = 0 if deleted_s == "-" else int(deleted_s)
            entry = files_acc.setdefault(file_id, {
                "churn_commits": 0, "churn_added": 0, "churn_deleted": 0,
                "last_commit_ts": None,
            })
            entry["churn_commits"] += 1
            entry["churn_added"] += added
            entry["churn_deleted"] += deleted
            if entry["last_commit_ts"] is None or current_ts > entry["last_commit_ts"]:
                entry["last_commit_ts"] = current_ts
            continue
        # anything else (unparseable line) is silently ignored

    return commits_scanned, files_acc


def _percentile_rank(sorted_pop: list[int], v: int) -> float:
    if not sorted_pop:
        return 0.0
    return bisect_right(sorted_pop, v) / len(sorted_pop)


_DEFINITIONS = {
    "churn_commits": "Count of commits touching the file within the churn "
                     "window (git log --numstat --since=<window_days> days "
                     "ago; full history when window_days is None). 0 for "
                     "files with no matching commits.",
    "churn_added": "Sum of added lines (git --numstat) across the counted "
                   "commits; binary-file numstat entries ('-') contribute 0.",
    "churn_deleted": "Sum of deleted lines (git --numstat) across the "
                     "counted commits; binary-file numstat entries ('-') "
                     "contribute 0.",
    "last_commit": "ISO date (YYYY-MM-DD, UTC) of the most recent counted "
                   "commit touching the file; '' if none.",
    "hotspot": "percentile_rank(churn_commits) * percentile_rank(cc_total), "
              "rounded to 4dp. percentile_rank(v) = |{x in pop: x <= v}| / n "
              "where pop is the metric's values over the analyzed files "
              "that have git churn data. 0.0 when churn_commits == 0, "
              "cc_total == 0, or the project has no git data.",
}


def apply_churn(result: AnalysisResult, churn: dict[str, Any]) -> None:
    """Merge ``collect_churn``'s output into ``result.metrics`` (in place).

    Must run after ``metrics.engine.compute_metrics`` -- it reads
    ``result.metrics["files"][fid]["cc_total"]`` for the hotspot formula
    and requires ``result.metrics["files"]`` / ``["definitions"]`` to
    already exist.
    """
    files_metrics: dict[str, dict[str, Any]] = result.metrics["files"]
    definitions: dict[str, str] = result.metrics.setdefault("definitions", {})
    definitions.update(_DEFINITIONS)

    result.metrics["churn"] = {
        "available": churn["available"],
        "window_days": churn["window_days"],
        "commits_scanned": churn["commits_scanned"],
        "reason": churn["reason"],
    }

    if not churn["available"]:
        result.metrics["hotspots"] = []
        return

    churn_files: dict[str, dict[str, Any]] = churn["files"]

    for fid, m in files_metrics.items():
        c = churn_files.get(fid)
        if c is not None:
            m["churn_commits"] = c["churn_commits"]
            m["churn_added"] = c["churn_added"]
            m["churn_deleted"] = c["churn_deleted"]
            m["last_commit"] = c["last_commit"]
        else:
            m["churn_commits"] = 0
            m["churn_added"] = 0
            m["churn_deleted"] = 0
            m["last_commit"] = ""

    have_churn_ids = sorted(fid for fid in files_metrics if fid in churn_files)
    commits_pop = sorted(files_metrics[fid]["churn_commits"] for fid in have_churn_ids)
    cc_pop = sorted(files_metrics[fid]["cc_total"] for fid in have_churn_ids)

    hotspots_list: list[dict[str, Any]] = []
    for fid, m in files_metrics.items():
        churn_commits = m["churn_commits"]
        cc_total = m["cc_total"]
        if churn_commits == 0 or cc_total == 0:
            hotspot = 0.0
        else:
            pr_commits = _percentile_rank(commits_pop, churn_commits)
            pr_cc = _percentile_rank(cc_pop, cc_total)
            hotspot = round(pr_commits * pr_cc, 4)
        m["hotspot"] = hotspot
        if hotspot > 0:
            hotspots_list.append({
                "file_id": fid,
                "hotspot": hotspot,
                "churn_commits": churn_commits,
                "cc_total": cc_total,
                "loc_code": m["loc_code"],
            })

    hotspots_list.sort(key=lambda x: (-x["hotspot"], x["file_id"]))
    result.metrics["hotspots"] = hotspots_list[:20]
