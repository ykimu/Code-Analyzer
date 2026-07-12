"""Tests for the local browser GUI (``code-analyzer gui``, codeanalyzer.gui.*).

Starts a real ``ThreadingHTTPServer`` (via ``codeanalyzer.gui.server.
create_server``) on an OS-assigned port for each test and drives it over
plain HTTP with ``urllib`` -- no test-only shortcuts into the handler, so
these tests exercise the exact code path a browser would.

``CODE_ANALYZER_GUI_STATE`` (an env var the server module reads instead of
hardcoding ``~/.code_analyzer_gui.json``) is monkeypatched per test so the
"recent analyses" persistence never touches the real user's home directory.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from codeanalyzer.gui import server as gui_server_mod
from codeanalyzer.gui.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample_project"

#: report.html embeds the gzip+base64 analysis payload as a JS assignment
#: (see tests/test_cli.py's identical regex) -- used here only to confirm
#: /report?job=ID serves a real report, not to decode the payload.
_B64_PAYLOAD_RE = re.compile(r'window\.__CA_B64__\s*=\s*"([^"]*)"')


# ---------------------------------------------------------------------------
# fixture: a live server on an isolated recent-file
# ---------------------------------------------------------------------------
@pytest.fixture
def gui(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_ANALYZER_GUI_STATE", str(tmp_path / "recent.json"))
    server = create_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(base: str, path: str, obj: dict) -> tuple[int, bytes]:
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        f"{base}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _poll_until_done(base: str, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        status, body = _get(base, f"/api/status?job={job_id}")
        assert status == 200
        data = json.loads(body)
        if data["done"]:
            return data
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s (last: {data})")


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------
def test_index_page_self_contained(gui):
    status, body = _get(gui, "/")
    assert status == 200
    text = body.decode("utf-8")
    assert "Code Analyzer" in text
    # no external network resources: everything (CSS/JS/fonts) is inlined
    assert "http://" not in text
    assert "https://" not in text


# ---------------------------------------------------------------------------
# GET /api/browse
# ---------------------------------------------------------------------------
def test_browse_defaults_to_home(gui):
    status, body = _get(gui, "/api/browse")
    data = json.loads(body)
    assert status == 200
    assert data["error"] is None
    assert data["path"] == str(Path.home().resolve())
    assert isinstance(data["dirs"], list)


def test_browse_subdir_lists_only_directories_sorted(gui, tmp_path):
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "not_a_dir.txt").write_text("x", encoding="utf-8")
    q = urllib.parse.quote(str(tmp_path))

    status, body = _get(gui, f"/api/browse?path={q}")
    data = json.loads(body)
    assert status == 200
    assert data["error"] is None
    assert data["dirs"] == ["a_dir", "b_dir"]
    assert data["parent"] == str(tmp_path.parent)


def test_browse_hidden_dirs_skipped_unless_requested(gui, tmp_path):
    (tmp_path / ".hidden_dir").mkdir()
    (tmp_path / "visible_dir").mkdir()
    q = urllib.parse.quote(str(tmp_path))

    status, body = _get(gui, f"/api/browse?path={q}")
    assert json.loads(body)["dirs"] == ["visible_dir"]

    status, body = _get(gui, f"/api/browse?path={q}&hidden=1")
    assert json.loads(body)["dirs"] == [".hidden_dir", "visible_dir"]


def test_browse_nonexistent_path_reports_error(gui):
    status, body = _get(gui, "/api/browse?path=/definitely/does/not/exist/xyz")
    data = json.loads(body)
    assert status == 200  # the endpoint itself always answers 200; the
    assert data["error"]  # failure is surfaced inside the JSON payload
    assert data["dirs"] == []


def test_browse_permission_error_is_handled_gracefully(monkeypatch, tmp_path):
    """PermissionError from the filesystem must not crash the request.

    Tests normally run as root (this sandbox included), where chmod-based
    permission denial can't be relied on to actually block access -- so we
    verify the guard directly at the unit level instead of over HTTP.
    """
    def _raise(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", _raise)
    dirs, error = gui_server_mod._list_subdirs(tmp_path, show_hidden=False)
    assert dirs == []
    assert error


# ---------------------------------------------------------------------------
# GET /api/status (unknown job)
# ---------------------------------------------------------------------------
def test_status_unknown_job_is_404_style_error(gui):
    status, body = _get(gui, "/api/status?job=job999")
    data = json.loads(body)
    assert status == 404
    assert data["error"]


# ---------------------------------------------------------------------------
# POST /api/analyze (bad path)
# ---------------------------------------------------------------------------
def test_analyze_nonexistent_path_returns_error_json(gui, tmp_path):
    status, body = _post(gui, "/api/analyze", {"path": str(tmp_path / "does-not-exist")})
    data = json.loads(body)
    assert status == 400
    assert data["error"]
    assert "job_id" not in data


# ---------------------------------------------------------------------------
# end-to-end: analyze -> poll -> report -> recent
# ---------------------------------------------------------------------------
def test_analyze_end_to_end_and_recent(gui, tmp_path):
    out_dir1 = tmp_path / "out1"
    status, body = _post(gui, "/api/analyze", {"path": str(SAMPLE), "out_dir": str(out_dir1)})
    assert status == 200
    job1 = json.loads(body)["job_id"]
    assert job1 == "job1"  # deterministic counter: first job on a fresh server

    data = _poll_until_done(gui, job1)
    assert data["error"] is None
    assert data["summary"] == {"files": 16, "parse_failures": 1, "unresolved_imports": 0}
    assert data["report_url"] == f"/report?job={job1}"
    assert data["out_dir"] == str(out_dir1)

    status, body = _get(gui, data["report_url"])
    assert status == 200
    html = body.decode("utf-8")
    assert _B64_PAYLOAD_RE.search(html), "report.html is missing the embedded payload"

    # a second analysis on the same server gets the next sequential id
    out_dir2 = tmp_path / "out2"
    status, body = _post(gui, "/api/analyze", {"path": str(SAMPLE), "out_dir": str(out_dir2)})
    job2 = json.loads(body)["job_id"]
    assert job2 == "job2"
    _poll_until_done(gui, job2)

    status, body = _get(gui, "/api/recent")
    assert status == 200
    recent = json.loads(body)
    assert len(recent) >= 2
    # most-recent-first
    assert recent[0]["out_dir"] == str(out_dir2)
    assert recent[0]["path"] == str(SAMPLE.resolve())
    assert recent[0]["report_exists"] is True
    assert recent[0]["summary"]["files"] == 16


def test_analyze_report_404_before_done(gui, tmp_path):
    # unknown job id -> /report also fails gracefully (never started)
    status, body = _get(gui, "/report?job=job-does-not-exist")
    assert status == 404
    assert json.loads(body)["error"]


# ---------------------------------------------------------------------------
# GET /api/report-file (whitelist-gated report serving for 最近の解析 rows)
# ---------------------------------------------------------------------------
def test_report_file_whitelisted_deleted_and_forbidden(gui, tmp_path):
    out_dir = tmp_path / "rf_out"
    status, body = _post(gui, "/api/analyze", {"path": str(SAMPLE), "out_dir": str(out_dir)})
    assert status == 200
    _poll_until_done(gui, json.loads(body)["job_id"])

    q = urllib.parse.quote(str(out_dir))

    # whitelisted (recorded in recent state) -> 200 with the real report HTML
    status, body = _get(gui, f"/api/report-file?path={q}")
    assert status == 200
    assert _B64_PAYLOAD_RE.search(body.decode("utf-8"))

    # non-whitelisted path -> 403, even if a report.html actually exists there
    rogue = tmp_path / "rogue"
    rogue.mkdir()
    (rogue / "report.html").write_text("<html>secret</html>", encoding="utf-8")
    status, body = _get(gui, f"/api/report-file?path={urllib.parse.quote(str(rogue))}")
    assert status == 403
    assert json.loads(body)["error"]

    # missing ?path= -> also refused
    status, body = _get(gui, "/api/report-file")
    assert status == 403
    assert json.loads(body)["error"]

    # whitelisted but the file has since been deleted -> 404
    (out_dir / "report.html").unlink()
    status, body = _get(gui, f"/api/report-file?path={q}")
    assert status == 404
    assert json.loads(body)["error"]


# ---------------------------------------------------------------------------
# gui.html static contract: submit must read path-input at submit time
# ---------------------------------------------------------------------------
def test_gui_html_reads_path_input_at_submit_time():
    """Regression guard for the "typed path silently ignored" defect.

    A user who types/pastes a path into #path-input and clicks 解析を実行
    without pressing Enter must still get that path analyzed. Simulating the
    stale-state browser flow server-side isn't possible, so assert the
    template's contract statically: runAnalyze() re-reads path-input's value
    and prefers it over the (possibly stale) state.currentPath, and the input
    also syncs on the "change" event.
    """
    template = (Path(gui_server_mod.__file__).parent / "template" / "gui.html")
    text = template.read_text(encoding="utf-8")

    run_analyze = text[text.index("function runAnalyze("):]
    run_analyze = run_analyze[:run_analyze.index("function pollStatus(")]
    assert '$("path-input").value' in run_analyze, (
        "runAnalyze() must read #path-input at submit time (defensive against "
        "a typed-but-not-Entered path)"
    )
    # and the typed value must actually feed the POSTed path
    assert "submitPath" in run_analyze and "path: submitPath" in run_analyze

    # the input also syncs on change/blur, not only on Enter
    assert '$("path-input").addEventListener("change"' in text


# ---------------------------------------------------------------------------
# recent-file corruption tolerance
# ---------------------------------------------------------------------------
def test_recent_corrupt_file_tolerated(gui, monkeypatch, tmp_path):
    corrupt_path = tmp_path / "corrupt_state.json"
    corrupt_path.write_text("{ this is not valid json !!", encoding="utf-8")
    monkeypatch.setenv("CODE_ANALYZER_GUI_STATE", str(corrupt_path))

    status, body = _get(gui, "/api/recent")
    assert status == 200
    assert json.loads(body) == []


def test_recent_missing_file_returns_empty_list(gui, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_ANALYZER_GUI_STATE", str(tmp_path / "does-not-exist.json"))
    status, body = _get(gui, "/api/recent")
    assert status == 200
    assert json.loads(body) == []
