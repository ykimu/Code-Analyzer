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
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from codeanalyzer.cli import DEFAULT_OUT_DIR, _git_revision, _validate_dir
from codeanalyzer.core.model import TOOL_VERSION
from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.metrics.engine import compute_metrics
from codeanalyzer.report.html_builder import build_html
from codeanalyzer.report.json_writer import write_json

#: Phase labels surfaced to the browser (see gui.html's phase indicator).
PHASE_SCAN = "走査・解析"
PHASE_METRICS = "メトリクス"
PHASE_REPORT = "レポート生成"
PHASE_DONE = "完了"
PHASE_ERROR = "エラー"

_TEMPLATE_DIR = Path(__file__).resolve().parent / "template"
_MAX_RECENT = 10


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


def _load_recent() -> list[dict[str, Any]]:
    """Load the persisted recent-analyses list; tolerate a missing/corrupt file."""
    path = _recent_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _save_recent(entries: list[dict[str, Any]]) -> None:
    path = _recent_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # best-effort persistence; a failure here must not crash the job


def _append_recent(entry: dict[str, Any]) -> None:
    entries = _load_recent()
    entries.append(entry)
    entries = entries[-_MAX_RECENT:]
    _save_recent(entries)


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

    options = {
        "include": include,
        "exclude": exclude,
        "no_gitignore": no_gitignore,
        "embed_sources": embed_sources,
        "max_nodes": max_nodes,
    }

    job_id = state.new_job(out_dir)
    thread = threading.Thread(
        target=_run_job, args=(state, job_id, p, out_dir, options), daemon=True,
    )
    thread.start()
    return 200, {"job_id": job_id}


def _run_job(state: GuiState, job_id: str, path: Path, out_dir: Path,
             options: dict[str, Any]) -> None:
    """Background job body -- mirrors ``cli.cmd_analyze``'s pipeline exactly."""
    job = state.jobs[job_id]
    try:
        job["phase"] = PHASE_SCAN
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
        })
    except Exception as e:  # noqa: BLE001 - background job error boundary
        job["error"] = str(e)
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


def _report_file_response(out_dir_str: Optional[str]) -> tuple[int, Optional[bytes], dict[str, Any]]:
    """Serve ``<out_dir>/report.html`` for a *recorded* recent analysis.

    Security: browsers block ``file://`` navigation from an ``http://`` page,
    so the 最近の解析 rows need an http URL -- but an endpoint that serves an
    arbitrary ``?path=`` would be a local file-disclosure hole. The requested
    ``out_dir`` must therefore EXACTLY match an ``out_dir`` recorded in the
    persisted recent-analyses state (a whitelist the server itself wrote);
    anything else is refused with 403. A whitelisted-but-deleted report file
    yields 404.

    Returns ``(status, html_bytes_or_None, error_obj)``.
    """
    if not out_dir_str:
        return 403, None, {"error": "path が指定されていません"}
    allowed = {entry.get("out_dir") for entry in _load_recent()}
    if out_dir_str not in allowed:
        return 403, None, {"error": "許可されていないパスです"}
    report_path = Path(out_dir_str) / "report.html"
    try:
        return 200, report_path.read_bytes(), {}
    except FileNotFoundError:
        return 404, None, {"error": "report.html が見つかりません"}
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
            self._send_json(200, _recent_list_with_status())
            return
        if parsed.path == "/api/report-file":
            out_dir_str = qs.get("path", [None])[0]
            status, html, err = _report_file_response(out_dir_str)
            if html is not None:
                self._send_html_bytes(status, html)
            else:
                self._send_json(status, err)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming convention
        parsed = urlsplit(self.path)
        if parsed.path == "/api/analyze":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "リクエストボディが不正なJSONです"})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "リクエストボディが不正です"})
                return
            status, obj = _start_analyze(self._state, body)
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
