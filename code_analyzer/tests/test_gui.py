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
import shutil
import subprocess
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
    payload = json.loads(body)
    recent = payload["entries"]
    assert len(recent) >= 2
    # most-recent-first
    assert recent[0]["out_dir"] == str(out_dir2)
    assert recent[0]["path"] == str(SAMPLE.resolve())
    assert recent[0]["report_exists"] is True
    assert recent[0]["summary"]["files"] == 16
    # v1.5: last_options reflects the most recently SUBMITTED analyze (job2
    # used no custom 詳細オプション, so this is the all-defaults shape).
    assert payload["last_options"] == {
        "include": "", "exclude": "", "no_gitignore": False,
        "embed_sources": True, "max_nodes": 800, "churn_window": 365,
        "out_dir": str(out_dir2),
    }


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
# churn option (v1.3: 履歴解析ウィンドウ(日), 0 = 無効)
# ---------------------------------------------------------------------------
def test_analyze_churn_window_zero_skips_churn(gui, tmp_path):
    # churn_window=0 must skip churn ENTIRELY server-side (0=無効): no
    # churn keys on any metrics row and no metrics["churn"] section --
    # never a "0 days" git log window.
    out_dir = tmp_path / "out_zero"
    status, body = _post(gui, "/api/analyze",
                          {"path": str(SAMPLE), "out_dir": str(out_dir), "churn_window": 0})
    assert status == 200
    data = _poll_until_done(gui, json.loads(body)["job_id"])
    assert data["error"] is None

    analysis = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    for fm in analysis["metrics"]["files"].values():
        assert "churn_commits" not in fm
        assert "hotspot" not in fm
    assert "churn" not in analysis["metrics"]
    assert "hotspots" not in analysis["metrics"]


# ---------------------------------------------------------------------------
# error_detail (v1.5): /api/status carries a traceback tail on a failed job
# ---------------------------------------------------------------------------
def test_status_error_detail_on_failed_job(gui, monkeypatch, tmp_path):
    # Hidden test-only hook (see server._start_analyze): a real mid-job
    # exception is otherwise hard to force deterministically without a racy
    # filesystem setup, so the server exposes a gated escape hatch instead.
    monkeypatch.setenv("CODE_ANALYZER_GUI_TEST", "1")
    out_dir = tmp_path / "out_fail"
    status, body = _post(gui, "/api/analyze",
                          {"path": str(SAMPLE), "out_dir": str(out_dir), "__test_fail__": True})
    assert status == 200
    job_id = json.loads(body)["job_id"]

    data = _poll_until_done(gui, job_id)
    assert data["error"] == "test failure"
    assert data["error_detail"]
    assert "test failure" in data["error_detail"]
    assert "RuntimeError" in data["error_detail"]


def test_test_fail_hook_inert_without_env_var(gui, tmp_path):
    # Without CODE_ANALYZER_GUI_TEST set, __test_fail__ must be a no-op --
    # the job runs normally (this guards against the hook ever being
    # reachable from a real client hitting a real server).
    out_dir = tmp_path / "out_not_fail"
    status, body = _post(gui, "/api/analyze",
                          {"path": str(SAMPLE), "out_dir": str(out_dir), "__test_fail__": True})
    assert status == 200
    data = _poll_until_done(gui, json.loads(body)["job_id"])
    assert data["error"] is None
    assert data["error_detail"] is None


# ---------------------------------------------------------------------------
# last_options round-trip (v1.5): 詳細オプション form values persist across
# GUI reopens, keyed off the persisted state file (never localStorage).
# ---------------------------------------------------------------------------
def test_last_options_round_trip(gui, tmp_path):
    out_dir = tmp_path / "custom_out"
    body = {
        "path": str(SAMPLE),
        "out_dir": str(out_dir),
        "include": "src/**/*.py",
        "exclude": "**/vendor/**",
        "no_gitignore": True,
        "embed_sources": False,
        "max_nodes": 250,
        "churn_window": 30,
    }
    status, resp = _post(gui, "/api/analyze", body)
    assert status == 200
    _poll_until_done(gui, json.loads(resp)["job_id"])

    status, resp = _get(gui, "/api/recent")
    assert status == 200
    last_options = json.loads(resp)["last_options"]
    assert last_options == {
        "include": "src/**/*.py",
        "exclude": "**/vendor/**",
        "no_gitignore": True,
        "embed_sources": False,
        "max_nodes": 250,
        "churn_window": 30,
        "out_dir": str(out_dir),
    }


def test_last_options_persisted_even_when_job_fails(gui, monkeypatch, tmp_path):
    # last_options reflects what the user SUBMITTED, independent of whether
    # the job later succeeds -- persisted at job start, not job completion.
    monkeypatch.setenv("CODE_ANALYZER_GUI_TEST", "1")
    out_dir = tmp_path / "out_fail_opts"
    status, resp = _post(gui, "/api/analyze", {
        "path": str(SAMPLE), "out_dir": str(out_dir),
        "max_nodes": 42, "__test_fail__": True,
    })
    assert status == 200
    _poll_until_done(gui, json.loads(resp)["job_id"])

    status, resp = _get(gui, "/api/recent")
    last_options = json.loads(resp)["last_options"]
    assert last_options["max_nodes"] == 42
    assert last_options["out_dir"] == str(out_dir)


# ---------------------------------------------------------------------------
# POST /api/compare (v1.3: snapshot-based temporal compare)
#
# Entries are identified by their `timestamp` field (a_ts/b_ts). Documented
# disambiguation decision (see server._start_compare): timestamps are the
# entry identity; if two recent entries ever share a timestamp (1-second
# resolution), the most recently recorded one wins, and a_ts == b_ts is
# always refused -- the UI keys its checkboxes by the same timestamp, so
# duplicate-timestamp entries are indistinguishable there too.
# ---------------------------------------------------------------------------
_CMP_PAYLOAD_RE = re.compile(r"window\.__CA_CMP__\s*=\s*(\{.*?\});\s*</script>", re.S)


def _run_job_and_wait(gui, path, out_dir) -> dict:
    status, body = _post(gui, "/api/analyze", {"path": str(path), "out_dir": str(out_dir)})
    assert status == 200
    return _poll_until_done(gui, json.loads(body)["job_id"])


def _recent_entries(gui) -> list:
    status, body = _get(gui, "/api/recent")
    assert status == 200
    return json.loads(body)["entries"]


def test_compare_temporal_happy_path(gui, tmp_path):
    """The primary use case: analyze -> edit code -> re-analyze the SAME
    path (same default-ish out_dir) -> compare the two runs. Works because
    each job snapshots its analysis.json; the second run overwriting
    out_dir/analysis.json must not matter."""
    proj = tmp_path / "proj"
    shutil.copytree(SAMPLE, proj)
    out_dir = tmp_path / "out"  # SAME out_dir for both runs, like the default

    _run_job_and_wait(gui, proj, out_dir)

    # Timestamps have 1-second resolution -- cross a second boundary so the
    # two entries are distinguishable (documented identity limitation).
    time.sleep(1.1)

    # edit the project in place: append a function body line
    target = proj / "py" / "models.py"
    target.write_text(target.read_text(encoding="utf-8")
                      + "\n\ndef extra_fn():\n    return 1\n")

    _run_job_and_wait(gui, proj, out_dir)

    recent = _recent_entries(gui)
    assert len(recent) >= 2
    # most-recent-first; both runs share path AND out_dir
    ts_new, ts_old = recent[0]["timestamp"], recent[1]["timestamp"]
    assert ts_new != ts_old
    assert recent[0]["out_dir"] == recent[1]["out_dir"] == str(out_dir)
    assert recent[0]["snapshot"] and recent[1]["snapshot"]
    assert recent[0]["snapshot"] != recent[1]["snapshot"]

    # a/b order is deliberately "wrong" (a=newer): the server must order
    # old/new by timestamp, not by the request keys.
    status, body = _post(gui, "/api/compare", {"a_ts": ts_new, "b_ts": ts_old})
    assert status == 200
    data = json.loads(body)
    assert data["compare_url"].startswith("/api/compare-file?name=cmp-")

    status, body = _get(gui, data["compare_url"])
    assert status == 200
    html = body.decode("utf-8")
    assert "__CA_CMP_JSON__" not in html
    m = _CMP_PAYLOAD_RE.search(html)
    assert m is not None, "embedded compare payload not found"
    cmp_data = json.loads(m.group(1))
    # the appended function changed py/models.py's metrics (old -> new,
    # i.e. loc/functions INCREASED -- confirms old/new were not swapped)
    changed = {c["file_id"]: c["deltas"] for c in cmp_data["files"]["changed"]}
    assert "py/models.py" in changed
    old_funcs, new_funcs = changed["py/models.py"]["functions"]
    assert new_funcs > old_funcs

    # compare artifacts live in the snapshots dir, not in out_dir
    snap_dir = Path(gui_server_mod._snapshots_dir())
    assert list(snap_dir.glob("cmp-*.json"))
    assert list(snap_dir.glob("cmp-*.html"))
    assert not (out_dir / "compare.json").exists()
    assert not (out_dir / "compare.html").exists()


def test_compare_unknown_timestamp_is_403(gui, tmp_path):
    out_a = tmp_path / "out_a"
    _run_job_and_wait(gui, SAMPLE, out_a)
    ts = _recent_entries(gui)[0]["timestamp"]

    status, body = _post(gui, "/api/compare",
                          {"a_ts": "2001-01-01T00:00:00+00:00", "b_ts": ts})
    assert status == 403
    assert json.loads(body)["error"]

    # a_ts == b_ts (same entry / indistinguishable duplicates) -> refused
    status, body = _post(gui, "/api/compare", {"a_ts": ts, "b_ts": ts})
    assert status == 400
    assert json.loads(body)["error"]


def test_compare_deleted_snapshot_is_clear_error(gui, tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(SAMPLE, proj)
    out_dir = tmp_path / "out"
    _run_job_and_wait(gui, proj, out_dir)
    time.sleep(1.1)
    _run_job_and_wait(gui, proj, out_dir)

    recent = _recent_entries(gui)
    ts_new, ts_old = recent[0]["timestamp"], recent[1]["timestamp"]
    # delete the older run's snapshot file out from under the server
    Path(recent[1]["snapshot"]).unlink()

    status, body = _post(gui, "/api/compare", {"a_ts": ts_old, "b_ts": ts_new})
    assert status == 400
    err = json.loads(body)["error"]
    assert "スナップショット" in err


def test_snapshot_pruning_on_recent_overflow(gui, tmp_path):
    # 11 successful jobs -> the recent list keeps 10; the pruned (oldest)
    # entry's snapshot file must be deleted, the surviving 10 kept.
    proj = tmp_path / "proj"
    shutil.copytree(FIXTURES / "impact_project", proj)  # small fixture: fast jobs
    out_dir = tmp_path / "out"

    _run_job_and_wait(gui, proj, out_dir)
    first_snapshot = _recent_entries(gui)[0]["snapshot"]
    assert first_snapshot and Path(first_snapshot).exists()

    for _ in range(10):
        _run_job_and_wait(gui, proj, out_dir)

    recent = _recent_entries(gui)
    assert len(recent) == 10
    surviving = [e["snapshot"] for e in recent]
    assert first_snapshot not in surviving       # oldest entry fell off
    assert not Path(first_snapshot).exists()     # ...and its file was pruned
    for snap in surviving:                       # survivors are intact
        assert snap and Path(snap).exists()


def test_compare_file_non_whitelisted_name_is_403(gui, tmp_path):
    # names the server did not generate itself are refused outright,
    # including traversal attempts -- whitelist, not sanitization.
    snap_dir = Path(gui_server_mod._snapshots_dir())
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "cmp-999.html").write_text("<html>rogue</html>", encoding="utf-8")

    status, body = _get(gui, "/api/compare-file?name=cmp-999.html")
    assert status == 403
    status, body = _get(gui, "/api/compare-file?name=" + urllib.parse.quote("../recent.json"))
    assert status == 403
    status, body = _get(gui, "/api/compare-file")
    assert status == 403


def test_report_file_evil_file_param_is_403(gui, tmp_path):
    out_a = tmp_path / "out_a"
    status, body = _post(gui, "/api/analyze", {"path": str(SAMPLE), "out_dir": str(out_a)})
    assert status == 200
    _poll_until_done(gui, json.loads(body)["job_id"])

    q = urllib.parse.quote(str(out_a))
    status, body = _get(gui, f"/api/report-file?path={q}&file=evil.html")
    assert status == 403
    assert json.loads(body)["error"]

    # path traversal via &file= is also refused (whitelist, not sanitization)
    status, body = _get(gui, f"/api/report-file?path={q}&file=../../etc/passwd")
    assert status == 403


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
# gui.html static contract (v1.5 GUI-UX pass): path autocomplete, Cmd+Enter
# shortcut, relative-time helper, and valid inline JS.
# ---------------------------------------------------------------------------
def _gui_html_text() -> str:
    template = Path(gui_server_mod.__file__).parent / "template" / "gui.html"
    return template.read_text(encoding="utf-8")


def test_index_page_has_path_datalist(gui):
    """#path-input must be wired to a <datalist> for path autocomplete
    (populated client-side from the recent-analyses list)."""
    status, body = _get(gui, "/")
    assert status == 200
    text = body.decode("utf-8")
    assert 'list="path-datalist"' in text
    assert '<datalist id="path-datalist">' in text


def test_gui_html_has_cmd_ctrl_enter_shortcut():
    """Cmd+Enter (mac) / Ctrl+Enter anywhere on the page must trigger
    解析を実行 -- a global keydown listener gated on metaKey/ctrlKey + Enter,
    and only when the run button is not already disabled (job in progress)."""
    text = _gui_html_text()
    assert 'e.metaKey || e.ctrlKey' in text
    assert "$(\"run-btn\").disabled" in text
    assert '$("run-btn").click()' in text
    # the button surfaces the shortcut to sighted and screen-reader users
    assert "Cmd+Enter" in text
    assert "Cmd+Enterでも実行できます" in text


def test_gui_html_has_relative_time_function():
    text = _gui_html_text()
    assert "function relativeTime(" in text
    assert "分前" in text and "日前" in text


def test_gui_html_has_error_detail_disclosure():
    text = _gui_html_text()
    assert "<details" not in text  # built dynamically, not hardcoded markup
    assert 'details.className = "error-detail"' in text
    assert 'summary.textContent = "詳細"' in text
    assert "error-detail-pre" in text
    assert "navigator.clipboard" in text


def test_gui_html_has_recent_filter_box():
    text = _gui_html_text()
    assert 'id="recent-filter"' in text
    assert 'id="recent-filter-row"' in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_gui_html_inline_script_is_valid_js(tmp_path):
    text = _gui_html_text()
    m = re.search(r"<script>(.*?)</script>", text, re.S)
    assert m is not None, "gui.html must have exactly one inline <script> block"
    js_path = tmp_path / "gui_inline.js"
    js_path.write_text(m.group(1), encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(js_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node --check failed:\n{result.stderr}"


def test_gui_html_has_no_typographic_dashes():
    # style/lint constraint for this GUI-UX pass: no em dash / en dash
    # characters anywhere in the template (plain ASCII hyphens only).
    text = _gui_html_text()
    assert "—" not in text  # em dash (—)
    assert "–" not in text  # en dash (–)


# ---------------------------------------------------------------------------
# recent-file corruption tolerance
# ---------------------------------------------------------------------------
def test_recent_corrupt_file_tolerated(gui, monkeypatch, tmp_path):
    corrupt_path = tmp_path / "corrupt_state.json"
    corrupt_path.write_text("{ this is not valid json !!", encoding="utf-8")
    monkeypatch.setenv("CODE_ANALYZER_GUI_STATE", str(corrupt_path))

    status, body = _get(gui, "/api/recent")
    assert status == 200
    payload = json.loads(body)
    assert payload["entries"] == []
    assert payload["last_options"] is None


def test_recent_missing_file_returns_empty_list(gui, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_ANALYZER_GUI_STATE", str(tmp_path / "does-not-exist.json"))
    status, body = _get(gui, "/api/recent")
    assert status == 200
    payload = json.loads(body)
    assert payload["entries"] == []
    assert payload["last_options"] is None
