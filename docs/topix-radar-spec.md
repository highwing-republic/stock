# 新TOPIX Watch Radar 実装仕様（Codex handoff）

作成: 2026-10-12（Claude Code による調査結果）。元仕様: `docs/watch-radar-spec.md`（JPX400 版）。
JPX400 版の仕組みをそのまま「新TOPIX」ユニバースへ横展開する。

---

## Goal

- EDINET 大量保有報告 × 価格 Setup のレーダーを、**新TOPIX 構成銘柄**を対象にもう 1 系統出力する。
- JPX400 版（`public/data/watch/`、`state/`）は**そのまま稼働**させ、出力・state を分離する。
- UI（makoto_consul リポジトリ）には `edinet-investment-radar/topix/` を新設し、既存 2 ページと同じ見た目で表示する。

## 現状アーキテクチャ（調査結果の要点）

- `src/watch/` はユニバースを `Universe`（membership JSON）経由でしか参照しない。
  `is_member / members_on / all_codes / company_name` の 4 API だけが本体から使われている。
- JPX400 固有なのは次の箇所だけ（それ以外は汎用）:
  - `src/watch/sources/jpx400.py`: JPX 公式 PDF の取得・解析、`CODE_RE` / `normalize_security_code` / `ConstituentsSnapshot`
  - `src/watch/universe.py`: シード日付定数（`DEFAULT_SEED_DATE=2026-08-31` など）
  - `src/watch/store.py`: `jpx400_membership.json` / `jpx400_adjustments.json` のパス
  - `src/watch/pipeline.py`: fetcher の import、`sources["jpx400"]`、ログ・エラー文言、`cfg.pipeline.jpx400_stale_days`
  - `src/watch/export.py`: `REASON_TEXT["UNIVERSE_EXIT"]="JPX400から除外された"`、`validate_public` の `sources` 必須キー
  - `src/watch/events.py`: フィールド名 `is_jpx400_at_event`
  - `src/watch/sources/margin.py`: エラー文言「JPX400 カバー率」
- v1（`scripts/update.py`）は EDINET の大量保有報告を**全銘柄分**取得して SQLite に入れている。
  ユニバース絞り込みは v2 側で行うため、**v1 の変更は不要**。
- 価格は `universe.all_codes()` 全部＋ `1306.T` を yfinance で 50 件ずつ取得（既存 prices テーブル共用）。
- 実行時間（2026-10-12 の Actions 実測）: v1 更新 8 分、watch 1 分（うち prices 23〜30 秒 / 約 440 銘柄）。
  新TOPIX（約 1,000 銘柄）でも prices は 2〜3 分、watch 全体で 5 分以内の見込み。

## 対象ユニバースの定義（決定事項・推奨）

JPX総研が 2026-10-07 に公表した「TOPIX初回定期入替における選定結果(2026年10月30日実施)及び構成銘柄一覧」の内容:

| 区分 | 件数 | 備考 |
|---|---|---|
| (1) 新規追加銘柄 | 35 | スタンダード 23・グロース 12。10/30 から構成銘柄 |
| (2) 移行措置銘柄 | 683 | 継続採用されず、10/30 以降 四半期ごと 8 段階でウエイト低減。2027-10 に再評価 |
| (3) 構成銘柄（2026-10-30 時点） | 1,669 | (2) を含む。(2) を除くと **986 銘柄** |

**推奨: 「新TOPIX」= (3) − (2) = 986 銘柄（移行措置銘柄を除く）** を既定とし、
`universe.include_transition: false` で切り替え可能にする。
UI では「新TOPIX（移行措置銘柄を除く）」と明記する。

在籍期間（effective_from）の扱い:

- 継続採用 951 銘柄（= 986 − 35）: 2026 年を通じて TOPIX 構成銘柄だった事実があるため、
  `effective_from = "2026-04-01"`, `seeded: true` で登録する（180 日リプレイ窓より前なら可。今日時点の窓開始は 2026-04-15）。
  これにより公開初日から Episode・ランキングが埋まる。
- 新規追加 35 銘柄: `effective_from = "2026-10-30"`, `seeded: false`。
- 移行措置 683 銘柄: `include_transition: false` のとき membership に**登録しない**
  （登録して `effective_to = 2026-10-29` にする案もあるが、10/29 までの Episode が UNIVERSE_EXIT で閉じる扱いになり
  状態変化欄がノイズだらけになるため、登録しない方を推奨）。
- ベンチマークは JPX400 版と同じ `1306.T`（TOPIX 連動 ETF）。新TOPIX にとっては自然な比較対象。

## データソース（確認済み。すべて JPX 公式）

1. **初回定期入替 PDF**（一次ソース。シード用）
   `https://www.jpx.co.jp/news/6030/t13vrt00000262wd-att/topix_j.pdf`（51 ページ、公表日 2026-10-07）
   - 見出し行: `（１）新規追加銘柄` / `（２）移行措置銘柄` / `（３）構成銘柄（2026年10月30日時点）` / 末尾 `（注）　銘柄コード順に掲載。…`
   - 明細行は `No. コード 市場区分`（例 `1 1301 プライム`）。**銘柄名列は無い**。
   - pypdf の `extract_text()` では 2 段組みの銘柄名（別表）が明細の間に混ざるため、
     `^\d+\s+(\d{3}[0-9A-Z])\s+(プライム|スタンダード|グロース)` の行だけ拾えば 35 / 683 / 1,669 件がぴったり取れる
     （検証済み。(2)(1) ⊂ (3)、重複なし）。市場区分の直後に銘柄名が連結された行が 7 行あるので正規表現は前方一致にする。
2. **TOPIX 構成銘柄ウエイト一覧 CSV**（継続更新用・銘柄名の補完用）
   `https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv`
   - エンコーディング **cp932**。ヘッダ `日付,銘柄名,コード,業種,TOPIXに占める個別銘柄のウエイト,ニューインデックス区分`。
   - 2026-10-12 時点で `日付=20260831` が 1,636 行。末尾に注記行（コード列が空）が付く。
   - 更新は「翌月末（最終営業日）16:20 以降」= **約 1 か月遅れ**。10/30 の入替結果が反映されるのは 11 月末ごろ。
   - 10/30 構成銘柄 1,669 のうち CSV に無いのは 38 件（新規 35 ＋ 641A/642A/646A など新規上場）。
3. **銘柄名の補完**: 上記 38 件は、v1 の SQLite `issuer_master`（EDINET コードリスト由来、全上場会社）から
   `security_code` で引く。無ければ CSV 更新時に埋める。暫定は証券コードを表示（UI は `companyName||securityCode` にフォールバック済み）。
4. **随時変更**: 上場廃止・再評価による復帰は、JPX400 版と同じ `adjustments` ファイル（人手確認済み JSON）で扱う。

JPX サイトは一般的な HTTP クライアントの既定 UA を 403 で弾くことがある。`sources/jpx400.py` と同じ `USER_AGENT` を使う。

## Relevant Files

stock リポジトリ:

- `config/watch.yaml`（既存）/ `config/watch-topix.yaml`（新規）
- `src/watch/config.py`（`universe` セクションの検証に `kind`, `label`, `include_transition` を追加）
- `src/watch/sources/common.py`（新規: `CODE_RE`, `normalize_security_code`, `ConstituentsSnapshot`, `USER_AGENT` を移動。`jpx400.py` からは再エクスポートして互換維持）
- `src/watch/sources/topix.py`（新規）
- `src/watch/universe.py`（シード関数の日付を引数化。`seed_topix(...)` 追加）
- `src/watch/store.py`（membership / adjustments パスを `kind` で切替）
- `src/watch/pipeline.py`（fetcher 選択、`sources` キー、文言）
- `src/watch/export.py`（`summary.universe` 追加、`UNIVERSE_EXIT` 文言、validate）
- `src/watch/events.py`（`is_jpx400_at_event` → `is_member_at_event`。state/events の再生成で 1 回だけ差分が出る。許容）
- `src/watch/sources/margin.py`（エラー文言のみ）
- `scripts/update_watch.py`（`--config/--state-dir/--out-dir` は既にある。説明文言のみ）
- `scripts/seed_topix.py`（新規・一回限り）
- `.github/workflows/update-radar.yml`（topix 用ステップ追加、`git add` 対象に `state/topix` は自動で含まれる）
- `tests/watch/`（新規: `test_topix_source.py`、既存 e2e に kind=topix のケース追加）
- `state/topix/`（新規: `topix_membership.json`, `topix_adjustments.json`, `topix_rebalance_2026.json`）
- `public/data/watch-topix/`（新規出力）

makoto_consul リポジトリ:

- `edinet-investment-radar/watch/index.html`, `watch/stock.html`（ユニバース表記を `summary.universe` から読むように汎用化）
- `edinet-investment-radar/topix/index.html`, `topix/stock.html`（新規。`DEFAULT_BASE` と meta だけ異なるコピー）
- `index.html`（ラボトップの「市場を見る」にカード追加、JSON-LD の ItemList に 1 件追加）
- `sitemap.xml` があれば追記

## Changes

### A. ユニバースの汎用化（stock）

1. `config/watch.yaml` の `universe` に `kind: jpx400`, `label: "JPX日経400"` を追加（既定値をコードにも持たせ、未指定なら jpx400）。
2. `config/watch-topix.yaml` を新規作成。`watch.yaml` の全文コピーに対し次だけ変更:
   `universe: {kind: topix, label: "新TOPIX", include_transition: false, min_members: 900}`、
   `pipeline.universe_stale_days: 45`（CSV が月次更新のため。`jpx400_stale_days` は `universe_stale_days` に改名し、旧キーも読む）。
3. `Store(root, kind)`: `membership_path = root/f"{kind}_membership.json"`、同様に adjustments。`kind` 既定 `"jpx400"`。
4. `pipeline.run`: `cfg.universe.kind` で fetcher を選ぶ（`jpx400.fetch_constituents` / `topix.fetch_constituents`）。
   `sources` には従来キーの代わりに `sources[kind]`（`{"status","lastSuccessAt","memberCount"}`）を入れる。
   JPX400 版の `sources["jpx400"]` は結果的に不変。
5. `export.build_public`: `summary["universe"] = {"key": kind, "label": label, "memberCount": universe_count,
   "note": "移行措置銘柄を除く" (topix かつ include_transition=false のときだけ)}` を追加。
   `validate_public` は `sources` に `edinet, prices, margin` と `summary["universe"]["key"]` があることを検査する。
   `REASON_TEXT["UNIVERSE_EXIT"]` は `f"{label}から除外された"` に（`PublicContext` に label を渡す）。
6. `events.py`: `is_jpx400_at_event` → `is_member_at_event`。テスト修正。
7. 文言: pipeline のログ・`QualityGateError("no JPX400 members …")`・margin の「JPX400 カバー率」を `label` 使用に。

### B. `src/watch/sources/topix.py`（新規）

```python
WEIGHT_CSV_URL = "https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv"
REBALANCE_PDF_URL_2026 = "https://www.jpx.co.jp/news/6030/t13vrt00000262wd-att/topix_j.pdf"

def parse_weight_csv(content: bytes, *, source_url) -> ConstituentsSnapshot
    # cp932 decode → csv → 日付が 8 桁数字の行だけ採用 → (normalize_security_code(コード), 銘柄名)
    # as_of = 日付 (YYYYMMDD) → date。件数 < min_members や重複は ValueError。
def fetch_constituents(session=None, *, timeout=30) -> ConstituentsSnapshot
def parse_rebalance_pdf_text(text: str) -> RebalanceLists   # new / transition / constituents (code, segment)
def parse_rebalance_pdf(content: bytes) -> RebalanceLists
```

- `RebalanceLists` は dataclass: `published_at: date(2026-10-07)`, `effective_date: date(2026-10-30)`,
  `new: tuple[(code, segment)]`, `transition: ...`, `constituents: ...`, `sha256`。
- 見出し判定は `（１）新規追加銘柄` / `（２）移行措置銘柄` / `（３）構成銘柄` の 3 行で区切る（全角数字）。
  明細は `^(\d+)\s+(\d{3}[0-9A-Z])\s+(プライム|スタンダード|グロース)` で拾う。
  件数検証: 新規 35 / 移行 683 / 構成 1669 と、`transition ⊂ constituents`, `new ⊂ constituents` を assert。
  数が合わないときは ValueError（解析ズレを黙って通さない）。

### C. シードと継続更新（universe.py / scripts/seed_topix.py）

- `scripts/seed_topix.py`:
  1. PDF（ローカルファイル指定可。CI では使わない）を解析 → `state/topix/topix_rebalance_2026.json` に
     `{schema_version:1, published_at, effective_date, new:[...], transition:[...], constituents:[...]}` を保存。
  2. CSV（最新）と v1 SQLite の `issuer_master` から銘柄名を引く。
  3. membership を生成: 継続 951 = constituents − transition − new を `effective_from=2026-04-01, seeded=true`、
     新規 35 を `effective_from=2026-10-30, seeded=false`。`transition` は `include_transition=false` なら登録しない。
     `last_as_of = "2026-10-30"`（PDF の時点）。保存先 `state/topix/topix_membership.json`。
  4. 空の `state/topix/topix_adjustments.json`（`{"schema_version":1,"adjustments":[]}`）を作る。
- 継続更新（pipeline 内、毎回）: CSV を取得し `apply_snapshot` を**除外方向だけ**に使う。
  - CSV の `as_of` が `last_as_of`（2026-10-30）より古い間は「情報が古い」として適用しない（既存の `last_as_of` ガードがそのまま効く）。
  - 2026-11 末以降、CSV が 10/30 以降の時点になったら差分適用。ただし `include_transition=false` のときは
    CSV にある移行措置銘柄を `rebalance_2026.json` の `transition` で除外してから渡す（追加されないようにする）。
  - 新規上場で CSV に増えた銘柄は従来どおり `effective_from = CSV の as_of` で追加する
    （新TOPIX ルール上の新規採用タイミングは JPX の案内に従う。疑わしければ adjustments で補正）。
- 2027-10 の再評価で復帰する銘柄は `topix_adjustments.json` に `add` で登録する運用（README に明記）。

### D. GitHub Actions

`Update watch radar (v2)` の直後に追加:

```yaml
      - name: Update watch radar (TOPIX)
        if: steps.key.outputs.configured == 'true'
        continue-on-error: true
        run: python scripts/update_watch.py --config config/watch-topix.yaml --state-dir state/topix --out-dir public/data/watch-topix
```

`git add public/data state` は既にディレクトリ指定なので変更不要。
スケジュールは現状維持。全体で 10 分→13 分程度を見込む。

### E. UI（makoto_consul）

1. `watch/index.html` / `watch/stock.html` を汎用化:
   - `summary.universe.label` があれば見出し・説明文・データ更新状況・meta の「JPX日経400」をその値に置換
     （無ければ従来文言。JPX400 版の後方互換）。
   - `sources` の表示は固定 4 キーではなく `edinet, prices, <universe.key>, margin` の順。ラベルは `universe.label + " 構成銘柄"`。
   - 用語集の「順位の決め方」等はユニバース非依存なのでそのまま。
2. `topix/index.html` / `topix/stock.html` を作成: `DEFAULT_BASE` を `.../public/data/watch-topix` に、
   `<title>` / description / canonical / og を新TOPIX 用に。本文は watch 版と同一（将来は共通 JS 化を検討）。
   ヘッダー一文: 「大株主が株式の5%超を新規取得・買い増しした企業（金融庁EDINETの大量保有報告）のうち、株価も上向きの銘柄だけを、
   新TOPIX構成銘柄（移行措置銘柄を除く約990銘柄）から毎日自動で絞り込みます。」
3. `index.html`（ラボトップ）にカード追加。JSON-LD の `ItemList` に `EDINET投資レーダー（新TOPIX）` を追加し `numberOfItems` を更新。
4. 2 ページ間の相互リンク（「JPX日経400 版はこちら」「新TOPIX 版はこちら」）をヘッダー直下に 1 行。

## Constraints

- `docs/watch-radar-spec.md` の原則は全て維持: 閾値は YAML、公開 JSON にスコア・推奨語を出さない、
  失敗時は前回出力と state を変更しない、同一入力でバイト一致。
- JPX400 版の `public/data/watch/` と `state/*.json` の**内容を変えない**（キー改名 `is_member_at_event` による
  `state/events/*.json` の 1 回限りの差分のみ許容。差分が出る PR では明記する）。
- TDnet は取得しない。JPX の無料データは `USER_AGENT` を付けて 1 日数回の取得に留める。
- SQLite は Actions cache。TOPIX 分の価格履歴追加で DB が増える（560 日 × 約 1,000 銘柄 ≒ 60 万行、数十 MB）。
  cache 上限（10 GB/リポジトリ）には余裕があるが、初回実行は yfinance 全件取得で時間がかかる。初回だけ手動 `workflow_dispatch` で様子を見る。

## Tests（`tests/watch/`。ネットワークには触らない）

- `test_topix_source.py`
  - CSV フィクスチャ（cp932、注記行付き、英数コード `285A` を含む）→ 行数・as_of・コード正規化・注記行の除外。
  - 行数が `min_members` 未満 → ValueError。
  - PDF テキストフィクスチャ（3 セクションの縮小版＋「プライム淺沼組」のような連結行）→ 3 リストの件数と包含関係。件数が宣言と合わないケースで ValueError。
- `test_universe.py` 追加: `seed_topix` で 継続/新規/移行 の effective_from が仕様どおり、`include_transition` 両方。
  CSV に移行措置銘柄が残っていても追加されないこと。CSV の as_of が古いときに適用されないこと。
- `test_pipeline_e2e.py` 追加: `kind=topix` で一式実行し、`summary.universe.key=="topix"`、`sources.topix` が存在、
  `stocks/*.json` が生成されること。JPX400 版の e2e が**変更なしで**通ること（出力バイト一致のテストが既にあるので流用）。
- `test_export.py`（または既存）: `UNIVERSE_EXIT` の文言が label 依存になっていること。禁止語検査に「新TOPIX」が引っかからないこと。
- `python -m pytest -q` 全体が緑。

## Acceptance Criteria

1. `python scripts/update_watch.py --config config/watch-topix.yaml --state-dir state/topix --out-dir public/data/watch-topix --offline`
   がローカルで成功し、`summary.json` の `universeCount` が 951（10/30 前）または 986（10/30 以降）になる。
2. 同コマンドを 2 回実行して `public/data/watch-topix/` と `state/topix/` が `updatedAt`・job_runs を除きバイト一致。
3. 移行措置 683 銘柄のコードが週次・月次ランキングと `stocks/` に 1 件も現れない（`include_transition=false`）。
4. JPX400 版の `public/data/watch/summary.json` が、この変更の前後で `updatedAt` 以外同一。
5. Actions の 1 回の実行が 15 分以内に完了し、両系統が commit される。
6. UI: `https://lab.ugatta-llc.com/edinet-investment-radar/topix/` が表示され、見出し・説明・データ更新状況に「新TOPIX」と表示される。
   `watch/` 側の表示は変わらない。スマホ幅で横スクロールが出ない。

## 決定が必要な点（推奨付き）

| # | 論点 | 推奨 |
|---|---|---|
| 1 | 移行措置 683 銘柄を含めるか | **含めない**（新TOPIX の趣旨に合う。YAML で切替可） |
| 2 | 継続銘柄の effective_from | **2026-04-01**（リプレイ窓より前。公開初日から結果が出る） |
| 3 | 10/30 より前の期間に新規 35 銘柄を対象にするか | **しない**（事実として構成銘柄でないため） |
| 4 | ページ URL | **`/edinet-investment-radar/topix/`**。JPX400 版は現状維持 |
| 5 | ラボトップの見せ方 | 既存カードの下に新TOPIX 版カードを追加（別ツール扱い） |

---

### 参考: 調査で使ったコマンド

```bash
# 10/7 PDF の 3 セクション件数検証（pypdf）
python -I -c "from pypdf import PdfReader; ..."   # 新規 35 / 移行 683 / 構成 1669、986 = 構成 − 移行
# ウエイト CSV
curl -A "Mozilla/5.0" -o topixweight_j.csv https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv
# cp932、1,636 行、日付 20260831
```
