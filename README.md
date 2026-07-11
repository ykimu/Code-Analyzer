# Code Analyzer

プロジェクトフォルダを指定して，ソースコードの依存関係・階層構造・ソフトウェアメトリクス・変更影響範囲を解析し，単一HTMLでインタラクティブに可視化するCLIツール．

対応言語: **Python / JavaScript / TypeScript / C / C++ / Java**（tree-sitterによる統一パース）

## セットアップ

```bash
cd code_analyzer
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Python 3.10以上が必要です．依存パッケージ（tree-sitter系とpathspec）は自動でインストールされます．

## 使い方

### 1. プロジェクト全体の解析とレポート生成

```bash
code-analyzer analyze /path/to/project -o out/
open out/report.html
```

`report.html` は完全オフラインの自己完結ファイルです（サーバ・CDN不要）．タブ構成:

- **概要** — サマリカード・言語構成・解析メタデータ・診断（パース失敗等）
- **依存グラフ** — 力学モデルの依存グラフ．検索/フィルタ/ズーム/ノード詳細/循環依存ハイライト/外部依存表示切替．大規模時はディレクトリ集約＋クリック展開
- **階層構造** — ディレクトリツリー＋ツリーマップ（サイズ=実効LOC，色=CC/MI切替）
- **メトリクス** — ファイル別テーブル（ソート可，中心性指標を含む），ネットワーク統計，CCワースト関数，MI分布，循環依存一覧，CSV/JSONダウンロード
- **影響範囲** — ファイルを選択するとソースコードが表示され，行をクリック（Shift+クリックで範囲拡張）して修正予定範囲を選択→影響範囲を可視化．影響ファイル名をクリックするとそのファイルの影響コード箇所がハイライト表示されます

主なオプション: `--include/--exclude GLOB`（対象調整），`--no-gitignore`，`--compile-commands FILE`（C/C++のインクルードパス解決），`--max-nodes N`（グラフ集約閾値，既定800），`--json-only`，`--no-embed-sources`（ソース埋め込み無効化でHTMLサイズ抑制）

### 2. 変更影響範囲の解析

```bash
code-analyzer impact /path/to/project src/app/service.py:42 --depth 5       # 単一行
code-analyzer impact /path/to/project src/app/service.py:42-58 --depth 5    # 行範囲
```

指定行が属する関数/クラスを特定し，(1) 呼び出し元・インポート元の推移的逆探索，(2) def-use連鎖によるデータフロー前方スライス（引数/戻り値経由の1レベル伝播）で影響ファイル・シンボル・行を列挙します．結果はテキスト・JSON・HTMLレポートに出力されます．

信頼度表示: **確実**（構文的に確実）/ **推定**（名前ベース解決・推移的到達）．動的ディスパッチ等で追跡が打ち切られた箇所は「未解決境界」として警告されます（偽陰性の可能性）．

### 3. メトリクスのみ表示

```bash
code-analyzer metrics /path/to/project --sort-by cc_max --top 20             # 整形テーブル
code-analyzer metrics /path/to/project --format csv --output metrics.csv     # CSVエクスポート
code-analyzer metrics /path/to/project --format md                           # Markdown表
code-analyzer metrics /path/to/project --format json --output metrics.json   # JSON
code-analyzer metrics /path/to/project --symbols-csv functions.csv           # 関数単位CSV
```

エクスポートにはネットワーク指標も含まれます．HTMLレポートのメトリクスタブからもCSV/JSONをダウンロードできます．

## メトリクス定義

LOC（物理/実効/コメント/空行を分離），循環的複雑度CC（1＋分岐点数），関数/クラス数，ファンイン/ファンアウト（内部/外部分離），循環依存（ファイル単位SCC），保守性指標MI（SEI版，0–100，ファイルのCC合計とHalstead Volume近似を使用）．

**ネットワーク指標**（内部インポートグラフ上で算出）: PageRank（減衰0.85，多くのファイルから推移的に依存される「基盤度」），媒介中心性（Brandes法，依存経路の「ボトルネック度」），近接中心性（調和平均，他ファイルからの到達しやすさ），次数中心性（正規化した入出次数）．全体統計として密度・平均次数・弱連結成分数も出力します．

正確な算出式は `analysis.json` の `metrics.definitions` に記録されます．言語間の数値比較には限界があるため，同一言語内での相対比較を推奨します．

## 制限事項（v1）

- 静的解析のため，動的インポート・リフレクション・関数ポインタ・動的ディスパッチは追跡できません（未解決境界として明示）
- C/C++はプリプロセスなしの構文レベル解析です（`compile_commands.json` でインクルードパスのみ反映可能）
- 言語をまたぐ呼び出し（FFI/JNI等）は解決対象外です
- シンボル解決は名前ベースであり，同名シンボルは候補集合として曖昧フラグ付きで報告されます
- 大きなMI値の低下はファイル単位CC合計を用いる定義に起因します（大規模ファイルはMIが0に張り付きやすい）

## 開発

```bash
PYTHONPATH=src python3 -m pytest tests/   # 119 tests
```

設計・仕様の詳細は `SPEC.md` を参照してください．
