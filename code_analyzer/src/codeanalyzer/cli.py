"""Command-line entry point (SPEC F8: CLI仕様, F9: 診断レポート).

Subcommands:

* ``analyze PATH``  -- run the full scan/parse/resolve/metrics pipeline and
  write ``analysis.json`` + ``report.html`` to an output directory. Sources
  are embedded into ``report.html`` by default (v1.1); pass
  ``--no-embed-sources`` to suppress that (smaller file, no source viewer /
  range selection in the 影響範囲 tab).
* ``impact PATH FILE:LINE[-END]`` -- run the pipeline plus change-impact
  analysis (SPEC F6), print a human-readable (Japanese) summary, and embed
  the impact result into the same JSON/HTML outputs. Also supports
  ``--no-embed-sources`` (see above).
* ``metrics PATH`` -- run the pipeline + metrics engine and print a
  per-file table (or ``--format json|md|csv``) to stdout (or ``--output
  FILE``).
* ``gui`` -- start a local (127.0.0.1-only) browser GUI (SPEC-adjacent
  convenience feature, v1.2) that wraps the same ``analyze`` pipeline so
  users never need to type CLI flags; see ``codeanalyzer.gui.server``.

Design notes / decisions (documented here since they are not fully pinned
down by SPEC.md's abbreviated usage grammar):

* Output directory (``-o/--out-dir``, default ``./code-analyzer-out``) is
  created if missing; existing ``analysis.json`` / ``report.html`` in it are
  overwritten silently (no confirmation prompt -- this is a CLI tool meant
  to be re-run repeatedly during development).
* ``--max-nodes N`` (analyze only, default 800): stored at
  ``result.metrics["ui"]["max_nodes"]`` before the JSON/HTML are written.
  The shipped ``report_template.html`` currently hardcodes its own
  aggregation threshold (``AGG_THRESHOLD = 800``) as a JS constant and does
  not read this key -- wiring the template up to it is out of scope for
  this module (owned by the report-template worker). The flag is still
  accepted and the value is still persisted in the JSON output, per the
  task instructions.
* Exit codes (uniform across subcommands): ``0`` success, ``2`` bad
  arguments / path / target that a human can fix, ``1`` unexpected
  (programming/environment) error. With ``-v/--verbose`` the full traceback
  is printed for the ``1`` case; otherwise a single concise message goes to
  stderr.
* Phase progress (走査/パース/解決 -> メトリクス -> レポート) is written to
  stderr and suppressed by ``-q/--quiet``; the end-of-run summary (SPEC F8:
  file count / parse failures / unresolved imports / output paths) is
  always printed (to stdout) regardless of ``-q``, since it is the actual
  deliverable of the command, not incidental progress noise.
* ``metrics --format table`` is a bespoke aligned-text renderer of the core
  LOC/CC/MI/fan columns; ``--sort-by``/``--top`` apply only to it.
  ``--format md|json|csv`` (v1.1) instead delegate to
  ``metrics.export.export_metrics(result, fmt)`` for consistency with the
  standalone export path -- these three render the *full* dataset
  (including the v1.1 network/centrality columns and the project-summary /
  cycles sections) and intentionally ignore ``--sort-by``/``--top``.
  ``--output FILE`` redirects any format's output to a file (stderr gets a
  confirmation line) instead of stdout; ``--symbols-csv FILE`` additionally
  writes ``metrics.export.export_symbols_csv(result)``.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

from codeanalyzer.core.model import TOOL_VERSION
from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.impact.analyzer import analyze_impact, format_impact_text
from codeanalyzer.impact.diff_impact import analyze_impact_diff
from codeanalyzer.metrics.churn import apply_churn, collect_churn
from codeanalyzer.metrics.engine import compute_metrics
from codeanalyzer.metrics.export import export_metrics, export_symbols_csv
from codeanalyzer.report.compare import build_compare_html, compare_analyses
from codeanalyzer.report.html_builder import build_html
from codeanalyzer.report.json_writer import write_json

#: FILE:LINE or FILE:START-END (SPEC F6). FILE may itself contain ':'
#: (e.g. a Windows drive letter) or '-'; the line spec is anchored at the
#: end of the string to keep the filename portion greedy but unambiguous.
_TARGET_RE = re.compile(r"^(?P<file>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")

_METRICS_COLUMNS = ["path", "lang", "loc_code", "funcs", "classes",
                     "cc_max", "mi", "fan_in", "fan_out"]
_METRICS_NUMERIC = {"loc_code", "funcs", "classes", "cc_max", "mi",
                     "fan_in", "fan_out"}

DEFAULT_OUT_DIR = "code-analyzer-out"


def _eprint(*args: Any, **kwargs: Any) -> None:
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _validate_dir(path_str: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve ``path_str`` and check it is an existing directory.

    Returns ``(path, None)`` on success or ``(None, message)`` on failure.
    """
    p = Path(path_str).expanduser()
    try:
        p = p.resolve()
    except OSError as e:  # pragma: no cover - defensive
        return None, f"パスを解決できません: {path_str} ({e})"
    if not p.exists():
        return None, f"パスが存在しません: {path_str}"
    if not p.is_dir():
        return None, f"ディレクトリではありません: {path_str}"
    return p, None


def _git_revision(root: Path) -> str:
    """Best-effort short git revision for ``root``; '' on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _run_pipeline(path: Path, quiet: bool, *, includes=None, excludes=None,
                   no_gitignore: bool = False, compile_commands: str | None = None,
                   run_churn: bool = False, churn_window: Optional[int] = 365):
    """Run scan/parse/resolve + metrics, injecting meta.generated_at/git_revision.

    Shared by all subcommands; ``includes``/``excludes``/``no_gitignore``/
    ``compile_commands`` only apply to ``analyze`` (the other subcommands
    use the pipeline defaults). ``run_churn`` (v1.3) additionally runs
    ``metrics.churn.collect_churn``/``apply_churn`` after the metrics phase
    -- ``churn_window`` is the ``--since`` window in days (``None`` = full
    history).
    """
    if not quiet:
        _eprint("[1/3] 走査/パース/解決 中...")
    result = analyze_project(
        path,
        includes=includes or [],
        excludes=excludes or [],
        respect_gitignore=not no_gitignore,
        compile_commands=compile_commands,
        keep_sources=True,
    )
    if not quiet:
        _eprint(f"[1/3] 完了: ファイル {len(result.files)} 件 "
                f"(パース失敗 {result.meta.parse_failures} 件)")
        _eprint("[2/3] メトリクス計算 中...")

    sources = getattr(result, "_sources", {})
    compute_metrics(result, sources)
    if not quiet:
        _eprint("[2/3] 完了")

    if run_churn:
        if not quiet:
            _eprint("履歴解析 中...")
        churn = collect_churn(path, window_days=churn_window)
        apply_churn(result, churn)
        if not quiet:
            _eprint(f"履歴解析 完了: {'利用可能' if churn['available'] else '利用不可 (' + str(churn['reason']) + ')'}")

    result.meta.generated_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    result.meta.git_revision = _git_revision(path)
    return result


def _write_outputs(result, out_dir: Path, json_only: bool, include_defuse: bool,
                    quiet: bool, embed_sources: bool = True) -> tuple[Path, Optional[Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "analysis.json"
    write_json(result, json_path, include_defuse=include_defuse)

    html_path: Optional[Path] = None
    if not json_only:
        if not quiet:
            _eprint("[3/3] レポート生成 中...")
        html_path = out_dir / "report.html"
        build_html(result, html_path, embed_sources=embed_sources)
        if not quiet:
            _eprint("[3/3] 完了")
    return json_path, html_path


def _print_end_summary(result, json_path: Path, html_path: Optional[Path]) -> None:
    meta = result.meta
    print("=== 解析サマリ ===")
    print(f"ファイル数: {meta.file_count}")
    print(f"パース失敗: {meta.parse_failures}")
    print(f"未解決インポート: {meta.unresolved_imports}")
    hotspots = (result.metrics or {}).get("hotspots") or []
    if hotspots:
        top3 = ", ".join(h["file_id"] for h in hotspots[:3])
        print(f"ホットスポット上位: {top3}")
    print(f"出力: {json_path}")
    if html_path is not None:
        print(f"      {html_path}")


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def cmd_analyze(args: argparse.Namespace) -> int:
    path, err = _validate_dir(args.path)
    if path is None:
        _eprint(f"エラー: {err}")
        return 2

    run_churn, churn_window, churn_err = _resolve_churn(args)
    if churn_err:
        _eprint(f"エラー: {churn_err}")
        return 2

    result = _run_pipeline(
        path, args.quiet,
        includes=args.include, excludes=args.exclude,
        no_gitignore=args.no_gitignore, compile_commands=args.compile_commands,
        run_churn=run_churn, churn_window=churn_window,
    )

    # SPEC F8 --max-nodes: see module docstring for the current limitation
    # (the shipped template does not yet read this key from the payload).
    result.metrics["ui"] = {"max_nodes": args.max_nodes}

    out_dir = Path(args.out_dir)
    json_path, html_path = _write_outputs(
        result, out_dir, args.json_only, args.with_defuse, args.quiet,
        embed_sources=not args.no_embed_sources)

    _print_end_summary(result, json_path, html_path)
    return 0


# ---------------------------------------------------------------------------
# impact
# ---------------------------------------------------------------------------
def cmd_impact(args: argparse.Namespace) -> int:
    path, err = _validate_dir(args.path)
    if path is None:
        _eprint(f"エラー: {err}")
        return 2

    has_target = args.target is not None
    has_diff = args.diff is not None
    if has_target and has_diff:
        _eprint("エラー: TARGET と --diff は同時に指定できません．いずれか一方のみを指定してください．")
        return 2
    if not has_target and not has_diff:
        _eprint("エラー: TARGET または --diff のいずれかを指定してください．")
        return 2

    if has_diff:
        # diff mode: run the full analyze pipeline (incl. churn), then
        # derive hunks from git and merge per-hunk impact results.
        result = _run_pipeline(path, args.quiet, run_churn=True, churn_window=365)
        impact = analyze_impact_diff(result, path, args.diff, max_depth=args.depth)
    else:
        m = _TARGET_RE.match(args.target)
        if not m:
            _eprint(
                f"エラー: TARGET は FILE:LINE または FILE:START-END の形式で指定してください "
                f"(入力値: {args.target!r})"
            )
            return 2
        file_arg = m.group("file")
        start_line = int(m.group("start"))
        end_group = m.group("end")
        end_line = int(end_group) if end_group is not None else None

        result = _run_pipeline(path, args.quiet)
        impact = analyze_impact(result, file_arg, start_line, end_line, max_depth=args.depth)

    if "error" in impact:
        _eprint(f"エラー: {impact['error']}")
        return 2

    result.impact = impact
    print(format_impact_text(impact))

    out_dir = Path(args.out_dir)
    json_path, html_path = _write_outputs(
        result, out_dir, args.json_only, False, args.quiet,
        embed_sources=not args.no_embed_sources)

    if not args.quiet:
        _eprint(f"出力: {json_path}")
        if html_path is not None:
            _eprint(f"      {html_path}")
    return 0


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def _metrics_rows(result) -> list[dict[str, Any]]:
    files_metrics = result.metrics.get("files", {})
    rows: list[dict[str, Any]] = []
    for f in result.files:
        fm = files_metrics.get(f.id, {})
        rows.append({
            "path": f.id,
            "lang": f.language,
            "loc_code": fm.get("loc_code", 0),
            "funcs": fm.get("functions", 0),
            "classes": fm.get("classes", 0),
            "cc_max": fm.get("cc_max", 0),
            "mi": round(fm.get("mi", 0.0), 1),
            "fan_in": fm.get("fan_in", 0),
            "fan_out": fm.get("fan_out", 0),
        })
    return rows


def _sort_rows(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    # stable double sort: ascending by path first (tie-breaker), then by the
    # requested column (descending for numeric columns, per task spec).
    rows = sorted(rows, key=lambda r: r["path"])
    reverse = sort_by in _METRICS_NUMERIC
    rows.sort(key=lambda r: r[sort_by], reverse=reverse)
    return rows


def _render_table(rows: list[dict[str, Any]]) -> str:
    widths = {}
    for h in _METRICS_COLUMNS:
        w = len(h)
        for r in rows:
            w = max(w, len(str(r[h])))
        widths[h] = w
    lines = ["  ".join(h.ljust(widths[h]) for h in _METRICS_COLUMNS)]
    lines.append("  ".join("-" * widths[h] for h in _METRICS_COLUMNS))
    for r in rows:
        lines.append("  ".join(str(r[h]).ljust(widths[h]) for h in _METRICS_COLUMNS))
    return "\n".join(lines)


def _render_md(rows: list[dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(_METRICS_COLUMNS) + " |",
             "|" + "|".join(["---"] * len(_METRICS_COLUMNS)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r[h]) for h in _METRICS_COLUMNS) + " |")
    return "\n".join(lines)


def _project_summary_line(project: dict[str, Any]) -> str:
    return (f"プロジェクト概要: ファイル {project.get('files', 0)} 件, "
            f"LOC(code) {project.get('loc_code', 0)}, "
            f"関数 {project.get('functions', 0)}, "
            f"クラス {project.get('classes', 0)}, "
            f"CC平均 {project.get('cc_avg', 0.0):.2f}, "
            f"MI平均 {project.get('mi_avg', 0.0):.1f}, "
            f"循環依存 {project.get('cycles_count', 0)} 件")


def _write_or_print(content: str, output: Optional[str], *, newline: Optional[str] = None) -> None:
    """Print ``content`` to stdout, or write it to ``output`` and confirm on stderr."""
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8", newline=newline)
        _eprint(f"出力: {out_path}")
    else:
        print(content)


def cmd_metrics(args: argparse.Namespace) -> int:
    path, err = _validate_dir(args.path)
    if path is None:
        _eprint(f"エラー: {err}")
        return 2

    if args.sort_by not in _METRICS_COLUMNS:
        _eprint(f"エラー: 不正な --sort-by 列: {args.sort_by!r} "
                f"(使用可能な列: {', '.join(_METRICS_COLUMNS)})")
        return 2
    if args.top is not None and args.top < 0:
        _eprint("エラー: --top は 0 以上の整数を指定してください")
        return 2

    run_churn, churn_window, churn_err = _resolve_churn(args)
    if churn_err:
        _eprint(f"エラー: {churn_err}")
        return 2

    # metrics-only run: no output files, so phase progress would just be
    # noise ahead of the table -- keep stderr quiet unconditionally.
    result = _run_pipeline(path, quiet=True,
                           run_churn=run_churn, churn_window=churn_window)

    if args.format == "table":
        # --sort-by/--top apply ONLY to the table renderer; csv/md/json go
        # through export_metrics() below and always render the full,
        # unsorted dataset (see module docstring / --format help text).
        rows = _sort_rows(_metrics_rows(result), args.sort_by)
        if args.top is not None:
            rows = rows[: args.top]
        project = result.metrics.get("project", {})
        content = _render_table(rows) + "\n\n" + _project_summary_line(project)
        _write_or_print(content, args.output)
    else:
        content = export_metrics(result, args.format)
        # csv content already uses RFC4180 CRLF line endings (via the csv
        # module) -- write it verbatim (newline="") so the platform's text
        # layer doesn't re-translate them.
        newline = "" if args.format == "csv" else None
        _write_or_print(content, args.output, newline=newline)

    if args.symbols_csv:
        sym_path = Path(args.symbols_csv)
        sym_path.parent.mkdir(parents=True, exist_ok=True)
        sym_path.write_text(export_symbols_csv(result), encoding="utf-8", newline="")
        _eprint(f"出力: {sym_path}")

    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
def _load_analysis_json(path: Path, label: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"{label} を読み込めません: {path} ({e})"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"{label} のJSON解析に失敗しました: {path} ({e})"
    return data, None


def cmd_compare(args: argparse.Namespace) -> int:
    old, err = _load_analysis_json(Path(args.old_json), "OLD_JSON")
    if err:
        _eprint(f"エラー: {err}")
        return 2
    new, err = _load_analysis_json(Path(args.new_json), "NEW_JSON")
    if err:
        _eprint(f"エラー: {err}")
        return 2

    try:
        cmp = compare_analyses(old, new)
    except ValueError as e:
        _eprint(f"エラー: {e}")
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "compare.json"
    json_path.write_text(json.dumps(cmp, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path: Optional[Path] = None
    if not args.json_only:
        html_path = out_dir / "compare.html"
        build_compare_html(cmp, html_path)

    files = cmp["files"]
    edges = cmp["edges"]
    cycles = cmp["cycles"]
    cycle_delta = len(cycles["added"]) - len(cycles["removed"])
    print("=== 比較サマリ ===")
    print(f"変更ファイル数: {len(files['changed'])}")
    print(f"追加ファイル数: {len(files['added'])}")
    print(f"削除ファイル数: {len(files['removed'])}")
    print(f"依存追加数: {len(edges['added'])}")
    print(f"依存削除数: {len(edges['removed'])}")
    print(f"循環増減: {cycle_delta:+d}")
    print(f"出力: {json_path}")
    if html_path is not None:
        print(f"      {html_path}")
    return 0


# ---------------------------------------------------------------------------
# gui
# ---------------------------------------------------------------------------
def cmd_gui(args: argparse.Namespace) -> int:
    # Imported lazily (rather than at module top) so that ``codeanalyzer.cli``
    # and ``codeanalyzer.gui.server`` -- which itself reuses several of this
    # module's helpers (``_git_revision``/``_validate_dir``/``DEFAULT_OUT_DIR``)
    # -- do not form a circular import at module load time.
    from codeanalyzer.gui.server import run_gui

    run_gui(port=args.port, open_browser=not args.no_browser)
    return 0


# ---------------------------------------------------------------------------
# argument parser
# ---------------------------------------------------------------------------
_NO_EMBED_SOURCES_HELP = (
    "ソースコードをHTMLレポートに埋め込まない"
    "（レポートの影響範囲タブでのソース表示・範囲選択が無効になる代わりに"
    "ファイルサイズを抑制）．"
)


def _add_embed_sources_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-embed-sources", action="store_true",
                   help=_NO_EMBED_SOURCES_HELP)


def _add_churn_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--churn-window", type=int, default=365, metavar="DAYS",
                   help="git変更履歴（チャーン/ホットスポット）の集計期間（日数，既定365）．"
                        "正の整数で期間を指定．0 を指定すると履歴解析そのものを無効化する"
                        "（--no-churn と同じ）．全履歴を対象にする場合は --churn-all を使用．")
    p.add_argument("--churn-all", action="store_true",
                   help="git変更履歴を期間で区切らず全履歴を対象に集計する"
                        "（--churn-window は無視される）．")
    p.add_argument("--no-churn", action="store_true",
                   help="git変更履歴の解析（チャーン/ホットスポット計算）を無効化する．")


def _resolve_churn(args: argparse.Namespace) -> tuple[bool, Optional[int], Optional[str]]:
    """Translate the churn CLI flags into ``(run_churn, window_days, error)``.

    Contract (model.py "v1.3 additions"): positive window_days = ``--since``
    window; ``None`` = full history (``--churn-all``); churn DISABLED
    (``--no-churn`` or ``--churn-window 0``) = collect_churn is never called
    and no churn keys appear in the metrics. Negative windows are a user
    error (exit 2 upstream, via the returned message).
    """
    if getattr(args, "no_churn", False):
        return False, None, None
    if getattr(args, "churn_all", False):
        return True, None, None
    window = args.churn_window
    if window == 0:
        return False, None, None
    if window < 0:
        return False, None, ("--churn-window は 0 以上の整数を指定してください"
                             "（0 = 履歴解析を無効化，全履歴は --churn-all）")
    return True, window, None


def _add_pipeline_scope_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--out-dir", default=DEFAULT_OUT_DIR,
                   help=f"出力先ディレクトリ（既定: ./{DEFAULT_OUT_DIR}）。"
                        "存在しなければ作成し，既存の analysis.json / report.html "
                        "は無警告で上書きされます．")
    p.add_argument("--json-only", action="store_true",
                   help="analysis.json のみ出力し report.html を生成しない．")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="進捗表示（走査/パース/解決・メトリクス・レポート）を抑制する．")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="予期しないエラー発生時にスタックトレース全体を表示する．")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-analyzer",
        description="多言語対応の静的解析ツール（依存関係・メトリクス・変更影響範囲）．",
        epilog="CLIフラグを覚えなくても使いたい場合は `code-analyzer gui` でローカル"
               "ブラウザGUI（127.0.0.1のみで動作）を起動してください．",
    )
    parser.add_argument("--version", action="version",
                        version=f"code-analyzer {TOOL_VERSION}")

    sub = parser.add_subparsers(dest="command")

    # -- analyze ----------------------------------------------------------
    p_analyze = sub.add_parser(
        "analyze", help="プロジェクトを解析し analysis.json / report.html を出力する．")
    p_analyze.add_argument("path", help="解析対象のプロジェクトルート")
    p_analyze.add_argument("--include", action="append", default=[],
                           metavar="GLOB", help="追加で含めるファイルのglob（複数指定可）")
    p_analyze.add_argument("--exclude", action="append", default=[],
                           metavar="GLOB", help="除外するファイルのglob（複数指定可）")
    p_analyze.add_argument("--no-gitignore", action="store_true",
                           help=".gitignore の尊重を無効化する．")
    p_analyze.add_argument("--compile-commands", metavar="FILE",
                           help="C/C++ 用 compile_commands.json のパス（インクルードパス解決に利用）．")
    p_analyze.add_argument("--max-nodes", type=int, default=800, metavar="N",
                           help="依存グラフをディレクトリ単位に自動集約するノード数閾値（既定800）．"
                                "metrics.ui.max_nodes として出力データに記録される．")
    p_analyze.add_argument("--with-defuse", action="store_true",
                           help="analysis.json に def-use（データフロー）レコードを含める．")
    _add_churn_args(p_analyze)
    _add_embed_sources_arg(p_analyze)
    _add_pipeline_scope_args(p_analyze)
    p_analyze.set_defaults(func=cmd_analyze)

    # -- impact -------------------------------------------------------------
    p_impact = sub.add_parser(
        "impact", help="指定した行（範囲）の変更影響範囲を解析する．")
    p_impact.add_argument("path", help="解析対象のプロジェクトルート")
    p_impact.add_argument("target", metavar="FILE:LINE[-END]", nargs="?", default=None,
                          help="影響範囲解析の起点（例: src/app.py:42 または src/app.py:10-20）．"
                               "--diff と同時指定不可（どちらか一方が必須）．")
    p_impact.add_argument("--diff", nargs="?", const="worktree", default=None, metavar="SPEC",
                          help="git diff に基づく差分駆動の影響範囲解析．SPEC省略時は"
                               "作業ツリーの未コミット変更（HEAD比較）．"
                               "SPECにはリビジョン（例: HEAD~1）や範囲（例: A..B）を指定可能．"
                               "TARGET と同時指定不可．")
    p_impact.add_argument("--depth", type=int, default=10, metavar="N",
                          help="逆依存探索の最大深度（既定10）．")
    _add_embed_sources_arg(p_impact)
    _add_pipeline_scope_args(p_impact)
    p_impact.set_defaults(func=cmd_impact)

    # -- metrics --------------------------------------------------------
    p_metrics = sub.add_parser(
        "metrics", help="ファイル別ソフトウェアメトリクスを表示する．")
    p_metrics.add_argument("path", help="解析対象のプロジェクトルート")
    p_metrics.add_argument("--format", choices=["table", "json", "md", "csv"],
                           default="table",
                           help="出力形式（既定: table）．table は整形済みテキスト表で "
                                "--sort-by/--top を適用する．md/json/csv は "
                                "export_metrics() によるデータセット全体の出力で，"
                                "--sort-by/--top は適用されない（ソートなし・全行）．")
    p_metrics.add_argument("--sort-by", default="path", metavar="METRIC",
                           help="ソート列（" + ", ".join(_METRICS_COLUMNS) + "）．"
                                "数値列は降順，それ以外は昇順（既定: path）．"
                                "table 形式にのみ適用される．")
    p_metrics.add_argument("--top", type=int, default=None, metavar="N",
                           help="表示する最大行数．table 形式にのみ適用される．")
    p_metrics.add_argument("--output", metavar="FILE",
                           help="出力を標準出力の代わりにファイルへ書き込む"
                                "（全フォーマット共通）．書き込み先は stderr に表示される．")
    p_metrics.add_argument("--symbols-csv", metavar="FILE",
                           help="関数/メソッドシンボル一覧を export_symbols_csv() の"
                                "CSV形式でファイルに出力する．")
    p_metrics.add_argument("-v", "--verbose", action="store_true",
                           help="予期しないエラー発生時にスタックトレース全体を表示する．")
    _add_churn_args(p_metrics)
    p_metrics.set_defaults(func=cmd_metrics)

    # -- compare ------------------------------------------------------------
    p_compare = sub.add_parser(
        "compare", help="2つの analysis.json を比較し compare.json / compare.html を出力する．")
    p_compare.add_argument("old_json", metavar="OLD_JSON", help="比較対象の古い analysis.json のパス")
    p_compare.add_argument("new_json", metavar="NEW_JSON", help="比較対象の新しい analysis.json のパス")
    p_compare.add_argument("-o", "--out-dir", default=DEFAULT_OUT_DIR,
                           help=f"出力先ディレクトリ（既定: ./{DEFAULT_OUT_DIR}）．")
    p_compare.add_argument("--json-only", action="store_true",
                           help="compare.json のみ出力し compare.html を生成しない．")
    p_compare.set_defaults(func=cmd_compare)

    # -- gui --------------------------------------------------------------
    p_gui = sub.add_parser(
        "gui", help="ローカルブラウザGUIを起動する（127.0.0.1のみ・CLIフラグ不要）．")
    p_gui.add_argument("--port", type=int, default=0, metavar="N",
                       help="待受ポート番号（既定: 0．OSが空きポートを自動割当）．")
    p_gui.add_argument("--no-browser", action="store_true",
                       help="起動時にブラウザを自動で開かない．")
    p_gui.set_defaults(func=cmd_gui)

    return parser


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001 - top-level CLI error boundary
        if getattr(args, "verbose", False):
            traceback.print_exc()
        else:
            _eprint(f"エラー: 予期しないエラーが発生しました: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
