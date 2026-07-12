"""Project file scanner (SPEC F1).

Recursively collects source files of supported languages, applying default
and user excludes, ``.gitignore`` rules, symlink/dedupe protection, size and
encoding guards, and test-file detection.  Output is deterministic.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

import pathspec

from codeanalyzer.core.model import EXTENSION_MAP, Diagnostic, Language

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

# Default excluded directory names (SPEC F1).
DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (
    "node_modules", "venv", ".venv", "env", ".git", ".hg", ".svn",
    "__pycache__", "build", "dist", "out", "target", "vendor", ".tox",
    ".mypy_cache", ".pytest_cache", "site-packages",
)

# Default excluded glob patterns matched against the relative posix path
# and/or the file name.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "*.min.js", "*_pb2.py", "*.generated.*",
)

# Directory name globs (covers ".*_cache" style entries e.g. .ruff_cache).
DEFAULT_EXCLUDE_DIR_GLOBS: tuple[str, ...] = (
    ".*_cache",
)


@dataclass
class ScannedFile:
    id: str            # relative posix path from root (stable id)
    path: Path         # absolute filesystem path
    language: Language
    size: int          # bytes
    source: str        # decoded text
    is_test: bool


def default_excludes() -> list[str]:
    """The exclude labels recorded in analysis metadata (reproducibility)."""
    return sorted(
        set(DEFAULT_EXCLUDE_DIRS)
        | set(DEFAULT_EXCLUDE_GLOBS)
        | set(DEFAULT_EXCLUDE_DIR_GLOBS)
    )


def _is_test_file(rel_posix: str) -> bool:
    """SPEC F5 test detection."""
    name = rel_posix.rsplit("/", 1)[-1]
    parts = rel_posix.split("/")
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    if name.startswith("test_"):
        return True
    stem = name
    # match *_test.*, *.spec.*, *.test.*
    dot = name.find(".")
    base = name[:dot] if dot != -1 else name
    if base.endswith("_test"):
        return True
    # segment-based checks against dotted name (foo.spec.ts -> {spec})
    segs = name.split(".")
    if len(segs) >= 3 and ("spec" in segs[1:-1] or "test" in segs[1:-1]):
        return True
    return False


def _dir_excluded(name: str, user_excludes: list[str]) -> bool:
    if name in DEFAULT_EXCLUDE_DIRS:
        return True
    for pat in DEFAULT_EXCLUDE_DIR_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return True
    for pat in user_excludes:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _glob_excluded(rel_posix: str, name: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_posix, pat):
            return True
        # support patterns like "dir/**" or path fragments
        if "/" in pat and fnmatch.fnmatch(rel_posix, pat):
            return True
    return False


def _load_gitignore(root: Path) -> pathspec.PathSpec | None:
    gi = root / ".gitignore"
    if not gi.is_file():
        return None
    try:
        lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    try:
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except (ValueError, LookupError):  # older pathspec fallback
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def scan(
    root: Path,
    includes: list[str],
    excludes: list[str],
    respect_gitignore: bool,
) -> tuple[list[ScannedFile], list[Diagnostic]]:
    """Walk ``root`` and return (scanned files, diagnostics), both sorted."""
    root = Path(root).resolve()
    includes = list(includes or [])
    excludes = list(excludes or [])
    diagnostics: list[Diagnostic] = []
    files: list[ScannedFile] = []
    seen_real: set[str] = set()

    gitspec = _load_gitignore(root) if respect_gitignore else None

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dpath = Path(dirpath)
        # Prune excluded / symlinked directories in place (deterministic order).
        kept = []
        for d in sorted(dirnames):
            full = dpath / d
            if full.is_symlink():
                continue
            if _dir_excluded(d, excludes):
                continue
            rel_dir = full.relative_to(root).as_posix()
            if gitspec is not None and gitspec.match_file(rel_dir + "/"):
                continue
            kept.append(d)
        dirnames[:] = kept

        for fname in sorted(filenames):
            full = dpath / fname
            if full.is_symlink():
                continue
            ext = full.suffix.lower()
            if ext not in EXTENSION_MAP:
                continue
            rel = full.relative_to(root).as_posix()

            # includes: if provided, file must match at least one.
            if includes and not any(
                fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(fname, p)
                for p in includes
            ):
                continue

            # default + user glob excludes
            if _glob_excluded(rel, fname, list(DEFAULT_EXCLUDE_GLOBS)):
                continue
            if excludes and _glob_excluded(rel, fname, excludes):
                continue
            if gitspec is not None and gitspec.match_file(rel):
                continue

            # dedupe by realpath (symlink-safe)
            try:
                real = os.path.realpath(full)
            except OSError:
                real = str(full)
            if real in seen_real:
                continue
            seen_real.add(real)

            try:
                size = full.stat().st_size
            except OSError as e:
                diagnostics.append(Diagnostic(rel, "skipped_size",
                                              f"stat failed: {e}"))
                continue
            if size > MAX_FILE_BYTES:
                diagnostics.append(Diagnostic(
                    rel, "skipped_size",
                    f"file is {size} bytes (>{MAX_FILE_BYTES} limit)"))
                continue

            try:
                raw = full.read_bytes()
            except OSError as e:
                diagnostics.append(Diagnostic(rel, "skipped_size",
                                              f"read failed: {e}"))
                continue

            try:
                source = raw.decode("utf-8")
            except UnicodeDecodeError:
                source = raw.decode("latin-1")
                diagnostics.append(Diagnostic(
                    rel, "encoding_fallback",
                    "UTF-8 decode failed; used latin-1 fallback"))

            files.append(ScannedFile(
                id=rel,
                path=full,
                language=EXTENSION_MAP[ext],
                size=size,
                source=source,
                is_test=_is_test_file(rel),
            ))

    files.sort(key=lambda f: f.id)
    diagnostics.sort(key=lambda d: (d.file, d.kind, d.line))
    return files, diagnostics
