"""Diff-driven change-impact analysis (v1.3 addition).

Instead of a single manually-supplied line range, this module derives the
changed line ranges from a git diff and feeds each analyzable hunk through the
existing :func:`codeanalyzer.impact.analyzer.analyze_impact`, then merges the
per-hunk results into ONE impact result following the ``analyze_impact_diff``
contract documented in ``core/model.py`` ("v1.3 additions").

Pure stdlib.  Output is fully deterministic (stable sorting throughout).

Two public entry points:

``extract_diff_hunks(root, diff_spec)``
    Run ``git diff -U0`` for the requested revision spec and parse the
    unified-0 output into a deterministic list of hunk records
    (``{"file", "start_line", "end_line", "status", "reason"}``).  Returns an
    ``{"error": ...}`` dict on any git failure.

``analyze_impact_diff(result, root, diff_spec, max_depth=10)``
    Analyze every analyzable hunk and merge the results.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from codeanalyzer.core.model import AnalysisResult, EXTENSION_MAP
from codeanalyzer.impact.analyzer import _group_affected, analyze_impact

# @@ -<oldstart>[,<oldcount>] +<newstart>[,<newcount>] @@ [context]
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# ---------------------------------------------------------------------------
# git invocation + unified-0 parsing
# ---------------------------------------------------------------------------
def _git_range_args(diff_spec: str) -> list[str]:
    """Translate a diff spec into the trailing args for ``git diff -U0``.

    * ``"worktree"``   -> ``HEAD`` (uncommitted changes vs HEAD)
    * ``"A..B"``/``"A...B"`` (explicit range) -> passed through unchanged
    * a single rev ``"HEAD~1"`` -> ``HEAD~1..HEAD``
    """
    if diff_spec == "worktree":
        return ["HEAD"]
    if ".." in diff_spec:
        return [diff_spec]
    return [f"{diff_spec}..HEAD"]


def extract_diff_hunks(root: Path, diff_spec: str) -> list[dict] | dict:
    """Return the analyzable/skipped hunks of ``diff_spec`` under ``root``.

    On any git failure a ``{"error": "<concise reason>"}`` dict is returned.
    """
    root = Path(root)
    cmd = ["git", "diff", "-U0", *_git_range_args(diff_spec)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True,
        )
    except (OSError, ValueError) as exc:  # git missing, bad cwd, ...
        return {"error": f"git diff failed: {exc}"}
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "git diff failed").strip()
        # collapse to the first line for a concise message
        reason = reason.splitlines()[0] if reason else "git diff failed"
        return {"error": f"git diff failed: {reason}"}
    return _parse_diff(proc.stdout)


def _parse_diff(text: str) -> list[dict]:
    """Parse ``git diff -U0`` output into a deterministic list of hunk dicts."""
    sections: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = {"new": None, "binary": False, "deleted": False, "hunks": []}
            sections.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                cur["deleted"] = True
            else:
                if path.startswith("b/"):
                    path = path[2:]
                cur["new"] = path
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = 1 if m.group(2) is None else int(m.group(2))
                cur["hunks"].append((start, count))

    out: list[dict] = []
    for sec in sections:
        # skip binary blobs, deletions (no new side), and mode/rename-only
        if sec["binary"] or sec["deleted"] or sec["new"] is None:
            continue
        file_id = PurePosixPath(sec["new"].replace("\\", "/")).as_posix()
        ext = os.path.splitext(file_id)[1].lower()

        if ext not in EXTENSION_MAP:
            for start, count in sec["hunks"]:
                end = start + max(count, 1) - 1
                out.append({
                    "file": file_id, "start_line": start, "end_line": end,
                    "status": "skipped", "reason": "unsupported language",
                })
            continue

        analyzable: list[tuple[int, int]] = []
        for start, count in sec["hunks"]:
            if count == 0:
                # pure deletion: the impacted symbol no longer exists on the
                # new side, so there is nothing to analyze
                end = start + max(count, 1) - 1  # == start
                out.append({
                    "file": file_id, "start_line": start, "end_line": end,
                    "status": "skipped", "reason": "deletion-only hunk",
                })
            else:
                analyzable.append((start, start + count - 1))

        for start, end in _merge_ranges(analyzable):
            out.append({
                "file": file_id, "start_line": start, "end_line": end,
                "status": "analyzed", "reason": None,
            })

    out.sort(key=lambda h: (h["file"], h["start_line"], h["end_line"],
                            h["status"]))
    return out


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping / adjacent (gap <= 1) inclusive ranges."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


# ---------------------------------------------------------------------------
# per-hunk analysis + merge
# ---------------------------------------------------------------------------
def analyze_impact_diff(result: AnalysisResult, root: Path, diff_spec: str,
                        max_depth: int = 10) -> dict:
    """Analyze every analyzable hunk of ``diff_spec`` and merge the results.

    See the ``analyze_impact_diff`` contract in ``core/model.py``.
    """
    hunks = extract_diff_hunks(root, diff_spec)
    if isinstance(hunks, dict):  # error dict passes through
        return {"error": hunks["error"]}

    hunk_records: list[dict] = []
    successes: list[tuple[dict, dict]] = []
    for h in hunks:
        if h["status"] != "analyzed":
            hunk_records.append(dict(h))
            continue
        imp = analyze_impact(result, h["file"], h["start_line"],
                             h["end_line"], max_depth)
        if "error" in imp:
            # e.g. blank-line-only hunk: keep going, record why it was skipped
            rec = dict(h)
            rec["status"] = "skipped"
            rec["reason"] = imp["error"]
            hunk_records.append(rec)
        else:
            hunk_records.append(dict(h))
            successes.append((h, imp))

    hunk_records.sort(key=lambda h: (h["file"], h["start_line"],
                                     h["end_line"], h["status"]))

    if not successes:
        return {"error": _no_hunks_reason(diff_spec, hunk_records)}

    # -- merge the per-hunk impact results --------------------------------
    origins: list[dict] = []
    seen_origins: set[str] = set()
    accum: dict[str, dict[str, Any]] = {}
    sliced: set[str] = set()
    boundaries: list[dict] = []
    seen_boundaries: set[tuple] = set()
    max_hops_reached = False

    for _h, imp in successes:
        origin = imp["origin"]
        if origin["symbol_id"] not in seen_origins:
            seen_origins.add(origin["symbol_id"])
            origins.append(origin)

        sliced.update(imp.get("sliced_vars", []))
        if imp.get("stats", {}).get("max_hops_reached"):
            max_hops_reached = True

        for af in imp.get("affected_files", []):
            for s in af["symbols"]:
                sid = s["symbol_id"]
                entry = accum.get(sid)
                if entry is None:
                    accum[sid] = {
                        "file_id": af["file_id"],
                        "lines": set(s["lines"]),
                        "confidence": s["confidence"],
                        "via": s["via"],          # first via wins
                        "hops": s["hops"],
                    }
                else:
                    entry["lines"].update(s["lines"])
                    entry["hops"] = min(entry["hops"], s["hops"])
                    if s["confidence"] == "certain":  # strongest wins
                        entry["confidence"] = "certain"
                    # via: keep the first cause, do not overwrite

        for b in imp.get("unresolved_boundaries", []):
            key = (b["symbol_id"], b["line"], b["reason"])
            if key not in seen_boundaries:
                seen_boundaries.add(key)
                boundaries.append(b)

    affected_files = _group_affected(accum)
    boundaries.sort(key=lambda b: (b["symbol_id"], b["line"], b["reason"]))

    return {
        "query": {"file": None, "start_line": None, "end_line": None,
                  "depth": int(max_depth), "diff": diff_spec},
        "origin": origins[0],
        "origins": origins,
        "affected_files": affected_files,
        "sliced_vars": sorted(sliced),
        "unresolved_boundaries": boundaries,
        "stats": {
            "files": len(affected_files),
            "symbols": sum(len(af["symbols"]) for af in affected_files),
            "max_hops_reached": max_hops_reached,
        },
        "hunks": hunk_records,
    }


def _no_hunks_reason(diff_spec: str, hunk_records: list[dict]) -> str:
    """Build the error message when no hunk was analyzable."""
    reasons: dict[str, int] = {}
    for h in hunk_records:
        if h["status"] == "skipped" and h.get("reason"):
            reasons[h["reason"]] = reasons.get(h["reason"], 0) + 1
    if reasons:
        summary = "; ".join(f"{r} (x{n})" for r, n in sorted(reasons.items()))
        return (f"no analyzable hunks in diff {diff_spec!r}: "
                f"all hunks skipped: {summary}")
    return f"no changed files with analyzable hunks in diff {diff_spec!r}"
