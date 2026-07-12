"""Local browser GUI HTTP server (``code-analyzer gui``).

Pure standard-library implementation: ``http.server.ThreadingHTTPServer`` +
``webbrowser`` + ``threading`` + ``json``. Binds to ``127.0.0.1`` only (never
``0.0.0.0``) -- this is a local developer convenience tool, not a service
meant to be reachable from the network.

The analysis job itself reuses the exact same building blocks as the
``analyze`` CLI subcommand (see ``codeanalyzer.cli``): ``analyze_project``
(SPEC F1-F4, ``keep_sources=True``) -> ``compute_metrics`` (SPEC F5) ->
``meta.generated_at``/``meta.git_revision`` injection (via
``codeanalyzer.cli._git_revision``, the same helper the CLI uses) ->
``write_json`` + ``build_html`` (SPEC F9). This keeps GUI-produced output
byte-for-byte equivalent to ``code-analyzer analyze`` output for the same
inputs; only the progress reporting differs (an in-memory job/phase dict
here instead of stderr lines).

Two entry points:

* :func:`create_server` -- builds a bound, not-yet-serving
  ``ThreadingHTTPServer`` (used directly by tests, which start/stop it on a
  background thread).
* :func:`run_gui` -- the CLI-facing entry point: creates the server, prints
  the URL, optionally opens a browser tab, and blocks in ``serve_forever()``
  until interrupted (Ctrl+C).
"""
from __future__ import annotations

import datetime
import itertools
import json
import os
import shutil
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from codeanalyzer.cli import DEFAULT_OUT_DIR, _git_revision, _validate_dir
from codeanalyzer.core.model import TOOL_VERSION
from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.metrics.churn import apply_churn, collect_churn
from codeanalyzer.metrics.engine import compute_metrics
from codeanalyzer.report.compare import build_compare_html, compare_analyses
from codeanalyzer.report.html_builder import build_html
from codeanalyzer.report.json_writer import write_json

#: Phase labels surfaced to the browser (see gui.html's phase indicator).
PHASE_SCAN = "走査・解析"
PHASE_METRICS = "メトリクス"
PHASE_CHURN = "履歴解析"
PHASE_REPORT = "レポート生成"
PHASE_DONE = "完了"
PHASE_ERROR = "エラー"

#: /api/report-file's ``&file=`` whitelist (v1.3: also serves compare.html).
_REPORT_FILE_WHITELIST = ("report.html", "compare.html")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "template"
_MAX_RECENT = 10

#: Guards read-modify-write cycles against the persisted state file (recent
#: entries + last_options): several code paths (a job's own completion, a
#: concurrent job's completion, and /api/analyze's last_options write at job
#: START) can all touch the same file from different threads.
_STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# persisted "recent analyses" store
# ---------------------------------------------------------------------------
def _recent_path() -> Path:
    """Path to the persisted recent-analyses JSON file.

    Overridable via the ``CODE_ANALYZER_GUI_STATE`` environment variable so
    tests do not touch the real user home directory.
    """
    override = os.environ.get("CODE_ANALYZER_GUI_STATE")
    if override:
        return Path(override)
    return Path.home() / ".code_analyzer_gui.json"


def _snapshots_dir() -> Path:
    """Directory holding per-job analysis.json snapshots (v1.3).

    Derived from the resolved recent-state file's directory (so the
    ``CODE_ANALYZER_GUI_STATE`` override keeps tests fully isolated):
    ``<dirname of state file>/code_analyzer_snapshots/``.

    Why snapshots exist: two analyze runs of the SAME project share the
    default ``out_dir`` (``<path>/code-analyzer-out``), so the second run
    overwrites the first ``analysis.json`` -- which would make the primary
    temporal-compare use case (analyze -> edit code -> re-analyze ->
    compare) impossible if compare read from ``out_dir``. Each successful
    job therefore copies its ``analysis.json`` here, and ``/api/compare``
    reads ONLY these snapshots.
    """
    return _recent_path().parent / "code_analyzer_snapshots"


def _snapshot_analysis(job_id: str, out_dir: Path) -> Optional[str]:
    """Copy ``out_dir/analysis.json`` into the snapshots dir; returns the
    absolute snapshot path (or None on any failure -- best-effort, a
    snapshot failure must not fail the job)."""
    try:
        snap_dir = _snapshots_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts_compact = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        snap_path = snap_dir / f"snap-{job_id}-{ts_compact}.json"
        shutil.copyfile(out_dir / "analysis.json", snap_path)
        return str(snap_path)
    except OSError:
        return None


def _load_state() -> dict[str, Any]:
    """Load the persisted GUI state: ``{"entries": [...], "last_options":
    {...} | None}``. Tolerates a missing file, a corrupt file, AND the
    pre-v1.5 format (a bare JSON list of entries, no ``last_options`` yet)."""
    path = _recent_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"entries": [], "last_options": None}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"entries": [], "last_options": None}
    if isinstance(data, list):  # pre-v1.5 bare-list format
        return {"entries": [e for e in data if isinstance(e, dict)], "last_options": None}
    if isinstance(data, dict):
        raw_entries = data.get("entries")
        entries = [e for e in raw_entries if isinstance(e, dict)] if isinstance(raw_entries, list) else []
        last_options = data.get("last_options")
        if not isinstance(last_options, dict):
            last_options = None
        return {"entries": entries, "last_options": last_options}
    return {"entries": [], "last_options": None}


def _save_state(state: dict[str, Any]) -> None:
    path = _recent_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # best-effort persistence; a failure here must not crash the job


def _load_recent() -> list[dict[str, Any]]:
    """Load the persisted recent-analyses list; tolerate a missing/corrupt file."""
    return _load_state()["entries"]


def _load_last_options() -> Optional[dict[str, Any]]:
    """Load the persisted 詳細オプション form values (v1.5), or ``None`` if
    no analyze has ever been run against this state file."""
    return _load_state()["last_options"]


def _persist_last_options(options: dict[str, Any]) -> None:
    """Persist the 詳細オプション form values so reopening the GUI restores
    them (v1.5). Called at job START (not completion) -- these are the
    values the user submitted, independent of whether the job later
    succeeds or fails."""
    with _STATE_LOCK:
        state = _load_state()
        state["last_options"] = options
        _save_state(state)


def _append_recent(entry: dict[str, Any]) -> None:
    with _STATE_LOCK:
        state = _load_state()
        entries = state["entries"]
        entries.append(entry)
        kept = entries[-_MAX_RECENT:]
        dropped = entries[:-_MAX_RECENT]
        if dropped:
            # prune snapshot files that no surviving recent entry references
            referenced = {e.get("snapshot") for e in kept if e.get("snapshot")}
            for e in dropped:
                snap = e.get("snapshot")
                if snap and snap not in referenced:
                    try:
                        Path(snap).unlink()
                    except OSError:
                        pass  # best-effort cleanup
        state["entries"] = kept
        _save_state(state)


def _recent_list_with_status() -> list[dict[str, Any]]:
    """Most-recent-first, each entry annotated with ``report_exists``."""
    entries = _load_recent()
    out = []
    for entry in reversed(entries):
        item = dict(entry)
        out_dir = entry.get("out_dir") or ""
        item["report_exists"] = bool(out_dir) and (Path(out_dir) / "report.html").is_file()
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# in-memory job registry
# ---------------------------------------------------------------------------
class GuiState:
    """Per-server in-memory state: job registry with a deterministic counter.

    Deliberately not a module-level global -- each :func:`create_server`
    call gets a fresh registry (starting at ``job1``), which keeps tests
    (and multiple GUI processes) independent of each other.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self._counter = itertools.count(1)
        self._cmp_counter = itertools.count(1)
        #: names of compare artifacts THIS server generated (cmp-<n>.html /
        #: cmp-<n>.json in the snapshots dir) -- the /api/compare-file
        #: whitelist. In-memory on purpose: a compare_url is used right
        #: away by the tab the click opened; it does not need to survive a
        #: server restart, and a restart-scoped whitelist can never be
        #: coaxed into serving a file the server did not itself write.
        self.compare_files: set[str] = set()
        self._lock = threading.Lock()

    def new_job(self, out_dir: Path) -> str:
        with self._lock:
            job_id = f"job{next(self._counter)}"
            self.jobs[job_id] = {
                "phase": PHASE_SCAN,
                "done": False,
                "error": None,
                "summary": None,
                "out_dir": str(out_dir),
            }
        return job_id

    def new_compare_name(self) -> str:
        """Reserve the next ``cmp-<n>`` artifact base name and whitelist
        both its .html and .json files."""
        with self._lock:
            base = f"cmp-{next(self._cmp_counter)}"
            self.compare_files.add(f"{base}.html")
            self.compare_files.add(f"{base}.json")
        return base


def _start_analyze(state: GuiState, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    path_str = body.get("path")
    if not isinstance(path_str, str) or not path_str.strip():
        return 400, {"error": "path は必須です"}

    p, err = _validate_dir(path_str)
    if p is None:
        return 400, {"error": err}

    out_dir_raw = body.get("out_dir") or None
    if out_dir_raw:
        out_dir = Path(str(out_dir_raw)).expanduser()
        try:
            out_dir = out_dir.resolve()
        except OSError:
            pass
    else:
        out_dir = p / DEFAULT_OUT_DIR

    include = str(body.get("include") or "").strip()
    exclude = str(body.get("exclude") or "").strip()
    no_gitignore = bool(body.get("no_gitignore", False))
    embed_sources = bool(body.get("embed_sources", True))
    try:
        max_nodes = int(body.get("max_nodes", 800))
    except (TypeError, ValueError):
        max_nodes = 800
    try:
        # 履歴解析ウィンドウ(日): 0 (or any non-positive value) disables
        # churn/hotspot analysis entirely -- see module docstring.
        churn_window = int(body.get("churn_window", 365))
    except (TypeError, ValueError):
        churn_window = 365

    options = {
        "include": include,
        "exclude": exclude,
        "no_gitignore": no_gitignore,
        "embed_sources": embed_sources,
        "max_nodes": max_nodes,
        "churn_window": churn_window,
    }

    # v1.5: persist the 詳細オプション form values (including out_dir, which
    # is not part of `options` above -- that dict feeds the analysis pipeline
    # and is also stored verbatim on the recent entry) so reopening the GUI
    # restores them. Persisted at job START -- these are the values the user
    # submitted, independent of whether the job later succeeds or fails.
    last_options = dict(options)
    last_options["out_dir"] = str(out_dir_raw) if out_dir_raw else ""
    _persist_last_options(last_options)

    # Hidden test-only hook (never reachable outside the test suite): lets
    # tests force a mid-job exception to exercise the error/error_detail
    # status path without racing the filesystem. Gated on an env var so it
    # can never be triggered by a real client hitting a real server.
    test_fail = bool(body.get("__test_fail__")) and bool(os.environ.get("CODE_ANALYZER_GUI_TEST"))

    job_id = state.new_job(out_dir)
    thread = threading.Thread(
        target=_run_job, args=(state, job_id, p, out_dir, options, test_fail), daemon=True,
    )
    thread.start()
    return 200, {"job_id": job_id}


def _run_job(state: GuiState, job_id: str, path: Path, out_dir: Path,
             options: dict[str, Any], test_fail: bool = False) -> None:
    """Background job body -- mirrors ``cli.cmd_analyze``'s pipeline exactly."""
    job = state.jobs[job_id]
    try:
        job["phase"] = PHASE_SCAN
        if test_fail:
            raise RuntimeError("test failure")
        includes = [options["include"]] if options["include"] else []
        excludes = [options["exclude"]] if options["exclude"] else []
        result = analyze_project(
            path,
            includes=includes,
            excludes=excludes,
            respect_gitignore=not options["no_gitignore"],
            keep_sources=True,
        )
        sources = getattr(result, "_sources", {})

        job["phase"] = PHASE_METRICS
        compute_metrics(result, sources)
        result.metrics["ui"] = {"max_nodes": options["max_nodes"]}
        result.meta.generated_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        result.meta.git_revision = _git_revision(path)

        job["phase"] = PHASE_CHURN
        churn_window = options.get("churn_window", 365)
        if churn_window and churn_window > 0:
            churn = collect_churn(path, window_days=churn_window)
            apply_churn(result, churn)

        job["phase"] = PHASE_REPORT
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json(result, out_dir / "analysis.json", include_defuse=False)
        build_html(result, out_dir / "report.html", embed_sources=options["embed_sources"])

        summary = {
            "files": result.meta.file_count,
            "parse_failures": result.meta.parse_failures,
            "unresolved_imports": result.meta.unresolved_imports,
        }
        job["summary"] = summary
        job["phase"] = PHASE_DONE
        job["done"] = True

        _append_recent({
            "path": str(path),
            "options": options,
            "out_dir": str(out_dir),
            "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "summary": summary,
            # v1.3: point-in-time copy of analysis.json for /api/compare
            # (out_dir is overwritten by the next run of the same project).
            "snapshot": _snapshot_analysis(job_id, out_dir),
        })
    except Exception as e:  # noqa: BLE001 - background job error boundary
        # v1.5: keep the last ~30 lines of the traceback for the UI's
        # collapsible 詳細 disclosure (a full traceback can be long; the
        # tail is what usually matters -- the raised exception itself).
        tb_lines = traceback.format_exc().splitlines()
        job["error"] = str(e)
        job["error_detail"] = "\n".join(tb_lines[-30:])
        job["phase"] = PHASE_ERROR
        job["done"] = True


def _status(state: GuiState, job_id: Optional[str]) -> tuple[int, dict[str, Any]]:
    job = state.jobs.get(job_id or "")
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    report_url = f"/report?job={job_id}" if job["done"] and not job.get("error") else None
    return 200, {
        "phase": job["phase"],
        "done": job["done"],
        "error": job.get("error"),
        "error_detail": job.get("error_detail"),
        "summary": job.get("summary"),
        "report_url": report_url,
        "out_dir": job["out_dir"],
    }


def _report_bytes(state: GuiState, job_id: Optional[str]) -> Optional[bytes]:
    job = state.jobs.get(job_id or "")
    if job is None or not job["done"] or job.get("error"):
        return None
    report_path = Path(job["out_dir"]) / "report.html"
    if not report_path.is_file():
        return None
    try:
        return report_path.read_bytes()
    except OSError:
        return None


def _report_file_response(out_dir_str: Optional[str],
                          file_name: str = "report.html") -> tuple[int, Optional[bytes], dict[str, Any]]:
    """Serve ``<out_dir>/<file_name>`` for a *recorded* recent analysis.

    Security: browsers block ``file://`` navigation from an ``http://`` page,
    so the 最近の解析 rows need an http URL -- but an endpoint that serves an
    arbitrary ``?path=`` would be a local file-disclosure hole. The requested
    ``out_dir`` must therefore EXACTLY match an ``out_dir`` recorded in the
    persisted recent-analyses state (a whitelist the server itself wrote);
    anything else is refused with 403. ``file_name`` (v1.3, ``&file=``) is
    itself whitelisted to ``report.html``/``compare.html`` -- anything else
    (including path-traversal attempts) is also a 403, never a filesystem
    lookup. A whitelisted-but-deleted report file yields 404.

    Returns ``(status, html_bytes_or_None, error_obj)``.
    """
    if not out_dir_str:
        return 403, None, {"error": "path が指定されていません"}
    if file_name not in _REPORT_FILE_WHITELIST:
        return 403, None, {"error": "許可されていないファイル名です"}
    allowed = {entry.get("out_dir") for entry in _load_recent()}
    if out_dir_str not in allowed:
        return 403, None, {"error": "許可されていないパスです"}
    report_path = Path(out_dir_str) / file_name
    try:
        return 200, report_path.read_bytes(), {}
    except FileNotFoundError:
        return 404, None, {"error": f"{file_name} が見つかりません"}
    except OSError as e:
        return 404, None, {"error": str(e)}


# ---------------------------------------------------------------------------
# /api/compare (v1.3: compare two recent analyses by snapshot)
# ---------------------------------------------------------------------------
def _load_snapshot(entry: dict[str, Any], label: str) -> tuple[Optional[dict], Optional[str]]:
    """Load a recent entry's snapshot JSON; ``(data, None)`` or ``(None, err)``."""
    snap = entry.get("snapshot")
    if not snap:
        return None, f"{label}の解析スナップショットが記録されていません（v1.3より前の解析結果は比較できません）"
    try:
        text = Path(snap).read_text(encoding="utf-8")
    except OSError:
        return None, f"{label}の解析スナップショットが見つかりません（削除された可能性があります）: {snap}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"{label}の解析スナップショットが壊れています: {snap} ({e})"


def _start_compare(state: GuiState, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Compare two *recorded* recent analyses, identified by their
    ``timestamp`` field (``{"a_ts": ..., "b_ts": ...}``), reading the
    per-job SNAPSHOT files -- never ``out_dir`` (which the next run of the
    same project overwrites; see :func:`_snapshots_dir`).

    Identity/disambiguation decision (documented): the ``timestamp``
    (second-resolution ISO 8601) is the entry's identity. If several recent
    entries share a timestamp, the LAST one in the persisted list (i.e. the
    most recently recorded) wins -- the UI keys its checkboxes by the same
    timestamp, so duplicate-timestamp entries are inherently
    indistinguishable there too, and collapsing to the newest matches what
    the user sees on top. ``a_ts == b_ts`` is therefore always refused.
    Which side is "old" vs. "new" is decided by comparing the two
    timestamps (ISO 8601 -> lexical == chronological), NOT by the a/b
    request order -- checkboxes can be ticked in either order.

    Timestamps not present in the recent list are refused with 403 (same
    whitelist philosophy as /api/report-file). Missing/corrupt snapshot
    files yield a clear 400 JSON error. compare.json/compare.html are
    written into the snapshots dir as ``cmp-<n>.json``/``cmp-<n>.html`` and
    served through the /api/compare-file whitelist.
    """
    a_ts = body.get("a_ts")
    b_ts = body.get("b_ts")
    if not a_ts or not b_ts:
        return 400, {"error": "a_ts と b_ts の両方を指定してください"}
    if a_ts == b_ts:
        return 400, {"error": "同一の解析結果（または同一タイムスタンプの解析結果）同士は比較できません"}

    by_ts: dict[str, dict[str, Any]] = {}
    for entry in _load_recent():
        ts = entry.get("timestamp")
        if ts:
            by_ts[ts] = entry  # last one wins (see docstring)

    if a_ts not in by_ts or b_ts not in by_ts:
        return 403, {"error": "許可されていない解析結果です（最近の解析一覧に存在しません）"}

    old_ts, new_ts = (a_ts, b_ts) if a_ts <= b_ts else (b_ts, a_ts)
    old_data, err = _load_snapshot(by_ts[old_ts], "古い側")
    if err:
        return 400, {"error": err}
    new_data, err = _load_snapshot(by_ts[new_ts], "新しい側")
    if err:
        return 400, {"error": err}

    try:
        cmp = compare_analyses(old_data, new_data)
    except ValueError as e:
        return 400, {"error": str(e)}

    snap_dir = _snapshots_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    base = state.new_compare_name()
    (snap_dir / f"{base}.json").write_text(
        json.dumps(cmp, ensure_ascii=False, indent=2), encoding="utf-8")
    build_compare_html(cmp, snap_dir / f"{base}.html")

    return 200, {"compare_url": f"/api/compare-file?name={base}.html"}


def _compare_file_response(state: GuiState,
                           name: Optional[str]) -> tuple[int, Optional[bytes], dict[str, Any]]:
    """Serve a server-generated compare artifact from the snapshots dir.

    ``name`` must exactly match a name this server instance itself wrote
    (``GuiState.compare_files``) -- anything else, including traversal
    attempts, is 403 without ever touching the filesystem.
    """
    if not name or name not in state.compare_files:
        return 403, None, {"error": "許可されていないファイル名です"}
    path = _snapshots_dir() / name
    try:
        return 200, path.read_bytes(), {}
    except FileNotFoundError:
        return 404, None, {"error": f"{name} が見つかりません"}
    except OSError as e:
        return 404, None, {"error": str(e)}


# ---------------------------------------------------------------------------
# folder browser
# ---------------------------------------------------------------------------
def _list_subdirs(path: Path, show_hidden: bool) -> tuple[list[str], Optional[str]]:
    try:
        children = list(path.iterdir())
    except PermissionError:
        return [], "アクセス権限がありません"
    except OSError as e:
        return [], str(e)

    names = []
    for entry in children:
        if not show_hidden and entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                names.append(entry.name)
        except OSError:
            continue  # unreadable entry (broken symlink etc.) -- skip silently
    names.sort(key=str.lower)
    return names, None


def _browse(path_str: str, show_hidden: bool) -> dict[str, Any]:
    p = Path(path_str or str(Path.home())).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass

    error: Optional[str] = None
    dirs: list[str] = []
    if not p.exists():
        error = f"パスが存在しません: {p}"
    elif not p.is_dir():
        error = f"ディレクトリではありません: {p}"
    else:
        dirs, error = _list_subdirs(p, show_hidden)

    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs, "error": error}


# ---------------------------------------------------------------------------
# gui.html rendering
# ---------------------------------------------------------------------------
def _render_gui_html() -> bytes:
    text = (_TEMPLATE_DIR / "gui.html").read_text(encoding="utf-8")
    text = text.replace("__VERSION__", TOOL_VERSION)
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _GuiRequestHandler(BaseHTTPRequestHandler):
    server_version = f"CodeAnalyzerGUI/{TOOL_VERSION}"

    # BaseHTTPRequestHandler logs every request to stderr by default; keep
    # the GUI process quiet (this is a local developer tool, not a service).
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # -- helpers ----------------------------------------------------------
    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html_bytes(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def _state(self) -> GuiState:
        return self.server.gui_state  # type: ignore[attr-defined]

    # -- routing ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming convention
        parsed = urlsplit(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/":
            self._send_html_bytes(200, _render_gui_html())
            return
        if parsed.path == "/api/browse":
            path_str = qs.get("path", [str(Path.home())])[0]
            show_hidden = qs.get("hidden", ["0"])[0] == "1"
            self._send_json(200, _browse(path_str, show_hidden))
            return
        if parsed.path == "/api/status":
            job_id = qs.get("job", [None])[0]
            status, obj = _status(self._state, job_id)
            self._send_json(status, obj)
            return
        if parsed.path == "/report":
            job_id = qs.get("job", [None])[0]
            data = _report_bytes(self._state, job_id)
            if data is None:
                self._send_json(404, {"error": "report not found"})
            else:
                self._send_html_bytes(200, data)
            return
        if parsed.path == "/api/recent":
            # v1.5: {"entries": [...], "last_options": {...} | None} -- was a
            # bare list pre-v1.5; last_options lets the GUI restore the
            # 詳細オプション form on reopen (see _persist_last_options).
            self._send_json(200, {
                "entries": _recent_list_with_status(),
                "last_options": _load_last_options(),
            })
            return
        if parsed.path == "/api/report-file":
            out_dir_str = qs.get("path", [None])[0]
            file_name = qs.get("file", ["report.html"])[0]
            status, html, err = _report_file_response(out_dir_str, file_name)
            if html is not None:
                self._send_html_bytes(status, html)
            else:
                self._send_json(status, err)
            return
        if parsed.path == "/api/compare-file":
            name = qs.get("name", [None])[0]
            status, html, err = _compare_file_response(self._state, name)
            if html is not None:
                self._send_html_bytes(status, html)
            else:
                self._send_json(status, err)
            return
        self._send_json(404, {"error": "not found"})

    def _read_json_body(self) -> tuple[bool, Any]:
        """Returns ``(ok, body_or_error_obj)``; ``ok=False`` -> already a
        ready-to-send ``{"error": ...}`` dict."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False, {"error": "リクエストボディが不正なJSONです"}
        if not isinstance(body, dict):
            return False, {"error": "リクエストボディが不正です"}
        return True, body

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming convention
        parsed = urlsplit(self.path)
        if parsed.path == "/api/analyze":
            ok, body = self._read_json_body()
            if not ok:
                self._send_json(400, body)
                return
            status, obj = _start_analyze(self._state, body)
            self._send_json(status, obj)
            return
        if parsed.path == "/api/compare":
            ok, body = self._read_json_body()
            if not ok:
                self._send_json(400, body)
                return
            status, obj = _start_compare(self._state, body)
            self._send_json(status, obj)
            return
        self._send_json(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------
def create_server(port: int = 0) -> ThreadingHTTPServer:
    """Build a bound (but not yet serving) GUI server on 127.0.0.1:``port``.

    ``port=0`` (the default) lets the OS pick a free port; read it back via
    ``server.server_address[1]``. Exposed separately from :func:`run_gui` so
    tests can start/stop it on a background thread without blocking.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _GuiRequestHandler)
    server.gui_state = GuiState()  # type: ignore[attr-defined]
    return server


def run_gui(port: int = 0, open_browser: bool = True) -> None:
    """Start the GUI server and block until interrupted (Ctrl+C).

    Prints the bound URL to stdout (``起動: http://127.0.0.1:PORT``) before
    blocking, and -- unless ``open_browser`` is false -- opens it in the
    user's default browser shortly after.
    """
    server = create_server(port)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"起動: {url}")

    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
