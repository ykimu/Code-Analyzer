# Code Analyzer 仕様書 (v1.1)

> **v1.1 追加**（スキーマ 1.1）:
> - **ネットワーク指標**: 内部インポートグラフ上の PageRank（減衰0.85）・媒介中心性（Brandes，(n-1)(n-2)正規化）・近接中心性（着信経路の調和平均）・次数中心性を全ファイルに算出し，グラフ全体統計（密度・平均次数・弱連結成分）を `metrics.network` に出力
> - **メトリクスエクスポート**: `metrics --format table|md|json|csv` ＋ `--output FILE`，関数単位の `--symbols-csv`．HTMLレポートからもCSV/JSONダウンロード可
> - **ソース連動の影響範囲UI**: ソースコードをHTMLに埋め込み（既定ON，`--no-embed-sources` で無効化，1ファイル200KB・合計20MBの上限ガード），影響範囲タブでファイル選択→ソース表示→行クリック/Shift+クリックで修正予定範囲を選択→複数起点シンボルの影響探索＋def-use対象変数のデータフローハイライト．影響ファイルのクリックで該当箇所を表示
> - CLIの `impact FILE:開始-終了` 行範囲指定（v1.0から対応済み）をUIと整合させ検証済み

対象プロジェクトのソースコードを静的解析し，依存関係・階層構造・ソフトウェアメトリクス・変更影響範囲を可視化するCLIツール．

## 1. 概要

| 項目 | 内容 |
|---|---|
| ツール名 | `code-analyzer`（Pythonパッケージ名 `codeanalyzer`） |
| 実装言語 | Python 3.10+ |
| 対象言語 | Python / JavaScript / TypeScript (TSX含む) / C / C++ / Java |
| パーサ | tree-sitter（全言語統一，言語別文法は pip wheel で導入） |
| 提供形態 | CLI ＋ 自己完結型単一HTMLレポート（サーバ・CDN不要，完全オフライン） |
| 実行環境 | macOS / Linux |

## 2. 機能要件

### F1. プロジェクト走査
- ルートフォルダを指定し，対象拡張子（`.py .js .jsx .mjs .cjs .ts .tsx .c .h .cpp .cc .cxx .hpp .hh .java`）のファイルを再帰的に収集する．
- 既定除外: `node_modules, venv, .venv, env, .git, .hg, .svn, __pycache__, build, dist, out, target, vendor, .tox, .mypy_cache, .pytest_cache, site-packages, *.min.js, *_pb2.py, *.generated.*`
- `.gitignore` を既定で尊重（`--no-gitignore` で無効化）．`--include GLOB` / `--exclude GLOB` で上書き可能．
- シンボリックリンクは辿らない（realpath正規化で同一実体の二重計上を防止）．
- 1ファイル5MB超はスキップし診断に記録．エンコーディングはUTF-8→失敗時latin-1フォールバック＋診断記録．
- 適用した除外設定は解析結果メタデータに記録する（再現性）．

### F2. 依存関係解析
- **インポート依存（ファイル単位）**: 各言語のインポート構文を抽出し，プロジェクト内ファイルへ解決する．
  - Python: `import` / `from ... import`（相対インポート対応）．プロジェクトルートおよびソースルート（`src/` 等）基準で解決．
  - JS/TS: `import` / `export from` / `require()` / `import()`．拡張子省略・`index.*` 解決・`tsconfig.json` の `baseUrl`/`paths` エイリアス対応．
  - C/C++: `#include "..."` は相対＋インクルードパス探索，`#include <...>` はプロジェクト内に見つかる場合のみ解決．`compile_commands.json` があればインクルードパスを反映（`--compile-commands` で明示指定可）．プリプロセスは行わない（構文レベル解析であることをレポートに明示）．
  - Java: `import` 文とパッケージ宣言をソースツリー基準で解決．同一パッケージ内の暗黙参照も解決対象．
- **シンボル依存（関数呼び出し・クラス参照）**: 関数/メソッド/クラス定義を抽出し，呼び出し式・継承・型参照からシンボル間エッジを構築する．
- 解決できないインポート・呼び出しは破棄せず **外部依存（External）** または **未解決（Unresolved）** として保持する．外部依存はパッケージ単位に集約して表示する．
- 言語をまたぐ呼び出し（FFI・JNI等）の解決は v1 スコープ外（未解決エッジとして表示）．

### F3. シンボル解決戦略
- シンボルは完全修飾名 FQN（`ファイル::クラス::関数` 形式）で一意化する．
- 呼び出し解決は次の優先順で行う: (1) 同一ファイル内スコープ → (2) インポート済みシンボル → (3) プロジェクト内の一意な同名シンボル．
- 一意化できない場合（同名複数候補）は候補集合すべてにエッジを張り `ambiguous` フラグを付与する．
- エッジには信頼度 `certain`（構文的に確実）/ `inferred`（名前ベース推定）/ `unresolved` を付与する．

### F4. ファイル階層構造の可視化
- ディレクトリツリー（折りたたみ可能）と，LOC/メトリクスに応じたサイズ・色分けのツリーマップの2表示を提供する．

### F5. ソフトウェアメトリクス（標準セット）
ファイル単位・シンボル単位・プロジェクト全体で算出し，算出定義をレポートに明記する:

| メトリクス | 定義 |
|---|---|
| LOC | 物理行・実効行（コード行）・コメント行・空行を分離集計 |
| 循環的複雑度 CC | 関数ごとに 1 + 分岐点数（if/for/while/case/catch/論理演算子/三項/comprehension）．ファイル値は関数の合計と最大 |
| 関数・クラス数 | 言語ごとの定義ノード数 |
| ファンイン/ファンアウト | ファイル単位の被依存数/依存数．内部・外部を分離集計 |
| 循環依存 | ファイル単位の強連結成分（SCC）検出．循環経路を列挙 |
| 保守性指標 MI | SEI版 `MI = max(0, min(100, (171 − 5.2·ln(HV) − 0.23·CC − 16.2·ln(LOC)) × 100/171))`（HVはHalstead Volume簡易算出） |

- 言語間の数値比較には限界がある旨をレポートに注記する（同一言語内の相対比較を推奨）．
- テストコード（`test_*, *_test.*, *.spec.*, *.test.*, tests/ 配下`）は分離集計する．

### F6. 影響範囲解析（データフロー追跡）
- 入力: `FILE:LINE` または `FILE:START-END`．
- 手順:
  1. 指定行を内包する最小の定義ノード（関数/メソッド/クラス）と文を特定する．空行・コメント行のみの場合は理由を提示してエラー．
  2. **後方影響（誰が影響を受けるか）**: 当該シンボルの呼び出し元・インポート元を逆依存グラフ上で推移的に探索（既定深度10，`--depth` で変更可）．
  3. **データフロー前方スライス**: 指定行で定義・変更される変数の def-use 連鎖を関数内で追跡し，戻り値・引数経由で呼び出し元/先へ伝播させる（関数間は1レベルの引数/戻り値対応で近似）．
  4. 影響を受ける各ファイルについて，影響シンボルと該当行範囲・信頼度（`certain`/`inferred`）を列挙する．
- 動的ディスパッチ・リフレクション・関数ポインタ等で追跡が打ち切られた箇所は `unresolved boundary` として明示する（偽陰性の可能性をユーザーに伝える）．
- 出力: CLIテキスト，JSON，およびHTMLレポート内のインタラクティブ表示（影響ノードのハイライトと該当コード断片表示）．

### F7. HTMLレポート（ブラウザUI）
単一の自己完結HTML（全JS/CSSインライン，D3.jsを同梱，解析データはgzip+Base64で埋め込み）．タブ構成:

1. **概要**: プロジェクトサマリ（ファイル数・LOC・言語構成・警告），解析メタデータ（ツール版・ルート・除外設定・日時・git revision）
2. **依存グラフ**: 力学モデルのインタラクティブグラフ．ズーム/パン，ノードクリックで詳細パネル（メトリクス・依存一覧），検索，言語/ディレクトリフィルタ，循環依存ハイライト，外部依存の表示切替．ノード数が閾値（既定800）を超える場合はディレクトリ単位に自動集約し，クリックで展開
3. **階層構造**: 折りたたみツリー＋ツリーマップ（サイズ=LOC，色=CC or MI 切替）
4. **メトリクス**: ソート可能なテーブル（ファイル/関数別），分布チャート，ワースト一覧，循環依存一覧
5. **影響範囲**: ファイル＋行を入力すると事前計算済みグラフ上で影響伝播を可視化（クライアント側で逆依存＋スライス結果を探索）．CLIで解析済みの結果も埋め込み表示

### F8. CLI仕様
```
code-analyzer analyze PATH [-o OUT_DIR] [--include G] [--exclude G] [--no-gitignore]
                            [--compile-commands FILE] [--max-nodes N] [--json-only]
code-analyzer impact PATH FILE:LINE[-END] [--depth N] [-o OUT_DIR] [--json-only]
code-analyzer metrics PATH [--format table|json|md] [--sort-by METRIC]
```
- `analyze` は `report.html` と `analysis.json` を出力する．
- フェーズ別進捗表示（走査/パース/解決/メトリクス/レポート）．終了時サマリ（成功N・パース失敗M・未解決シンボル数）．
- 出力は決定的（安定ソート）とし，同一入力からは同一出力を得る．

### F9. 診断レポート
パース失敗・部分成功・スキップ（サイズ超過/エンコーディング）・未解決インポートをファイル一覧として `analysis.json` とHTMLの概要タブに記録する．失敗ファイルは解析から隔離し，全体処理は続行する．

## 3. 非機能要件
- **性能**: 1,000ファイル規模を1分以内（目安）．tree-sitterパースは逐次で十分高速．
- **HTMLサイズ**: 目安50MB以下．超過リスク時はシンボルレベルエッジを間引き，警告を表示．
- **依存**: `tree-sitter` と言語wheel群，`pathspec`（gitignore処理）のみ．UIはD3.js（同梱・ISC License）以外の外部依存なし．
- **テスト**: pytest．言語別フィクスチャによる単体テスト＋本ツール自身を解析するセルフテスト．

## 4. アーキテクチャ

```
codeanalyzer/
├── cli.py                 # argparse エントリポイント
├── core/
│   ├── model.py           # データモデル（IR）: FileInfo, Symbol, Edge, Diagnostic, AnalysisResult
│   ├── scanner.py         # ファイル走査・除外処理
│   └── graph.py           # 依存グラフ構築・SCC・逆依存探索
├── parsers/
│   ├── base.py            # LanguageParser 抽象クラス（tree-sitter共通処理）
│   ├── python_parser.py / js_ts_parser.py / c_cpp_parser.py / java_parser.py
├── resolvers/             # 言語別モジュール解決（tsconfig, 相対import, include path, package）
├── metrics/engine.py      # メトリクス算出
├── impact/analyzer.py     # 影響範囲解析（逆依存＋def-useスライス）
└── report/
    ├── json_writer.py     # analysis.json（スキーマ versioned）
    ├── html_builder.py    # テンプレートへのデータ埋め込み
    └── template/          # 単一HTMLテンプレート＋同梱D3
```

### データフロー
`scan → parse(言語別AST) → extract(シンボル/インポート/呼び出し/def-use) → resolve(モジュール・シンボル解決) → graph → metrics → impact(要求時) → report(JSON/HTML)`

## 5. v1スコープ外（明示）
言語横断の呼び出し解決（FFI/JNI），C/C++の完全プリプロセス・テンプレート実体化追跡，型推論ベースの厳密なデータフロー解析，増分解析（簡易ハッシュキャッシュのみ実装），CI連携，Git履歴解析．
