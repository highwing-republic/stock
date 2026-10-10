# EDINET投資レーダー

金融庁EDINET API v2の大量保有報告書・変更報告書と、yfinanceの日足OHLCVを
結び付け、宿泊DXラボ内の未リンク画面向けJSONを生成する個人利用MVPです。

## 構成

- `src/radar.py`: EDINET取得、コード変換、CSV解析、SQLite、株価、指標、JSON出力
- `scripts/update.py`: 一括更新。1件の失敗で他の書類・銘柄を止めません
- `public/data`: 公開用JSON。APIキーやSQLiteは含みません
- `.github/workflows/update-radar.yml`: 平日4回の定期更新

ブラウザからEDINETやyfinanceへ直接アクセスしません。対象会社は必ず
`issuerEdinetCode`をEDINETコードリストで変換し、提出者の`secCode`は使いません。

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env`または実行環境へ`EDINET_API_KEY`を設定してください。値はログ、JSON、
HTMLへ出力されません。

```bash
# 通常更新（直近7日）
EDINET_API_KEY=... python scripts/update.py

# 初回90日
EDINET_API_KEY=... python scripts/update.py --days 90

# 任意期間
EDINET_API_KEY=... python scripts/update.py --start 2026-01-01 --end 2026-09-14
```

DBは既定で`data/investment_radar.db`、Web JSONは`public/data/`です。DB内の
`doc_id`と価格の複合主キーで再実行を安全にしています。

## GitHub Actions

`highwing-republic/stock`のSettings → Secrets and variables → Actionsに
`EDINET_API_KEY`を登録します。既に同名Secretがあれば追加作業は不要です。
Actionsは平日09:30、12:30、15:30、18:00（JST）に更新し、変更されたJSONだけを
mainへコミットします。手動実行では`days`（既定90）を指定できます。

## v2: JPX400 Watch Radar

大量保有報告（EDINET）とJPX400限定の価格Setupを組み合わせ、週次TOP5・月次TOP10を
`public/data/watch/`へ出力します。v1のJSON（`public/data/latest.json`等）は変更しません。
仕様は`docs/watch-radar-spec.md`、閾値はすべて`config/watch.yaml`にあります。

- `src/watch/`: 設定・営業日・イベント正規化・価格/指標・Setup・Episode状態機械・ランキング・成績・出力
- `scripts/update_watch.py [--as-of YYYY-MM-DD] [--offline]`: v1のDBを読み、JPX400構成・価格・信用残
  （任意）を更新して計算します。`--offline`はネットワーク取得をせずDBと`state/`だけで再計算します
- `state/`: 履歴の正本（Git管理）。JPX400 membership、正規化イベント、Episode、Setup履歴、
  週次/月次ランキング（期間終了後は`final: true`で凍結）、job_runs。SQLiteは再構築可能なキャッシュ
- Episode・ランキングは`pipeline.replay_days`（既定180日）の日次リプレイで毎回決定的に計算します。
  同じ入力なら`state/`と公開JSONはバイト単位で同一です（`updatedAt`とjob_runsを除く）
- 最新営業日の価格カバレッジが`prices.min_coverage`未満、またはJPX400が空の場合は公開JSONとstateを
  変更しません（job_runsに`failed`を記録）。公開JSONにスコアや推奨表現は含めません
- 環境変数`DB_PATH` / `WATCH_STATE_DIR` / `WATCH_OUT_DIR`で入出力先を切り替えられます
- TDnetは取得しません。決算イベントは対象外です

## v3: TOPIX Watch Radar（同一パイプライン・別ユニバース）

v2 の判定ロジックをそのまま、ユニバースを新 TOPIX（2026-10-30 初回定期入替後の 986 銘柄 = 構成銘柄 1,669 −
移行措置 683）に差し替えて実行します。設定は `config/watch-topix.yaml`（`watch.yaml` の上に差分だけ重ねる）、
state は `state/topix/`、公開 JSON は `public/data/watch-topix/`（スキーマは v2 と同じ。`sources.topix` が
ユニバースの状態）。JPX400 側のファイル・出力は変わりません。

- `src/watch/sources/topix.py`: 選定結果 PDF（初回シード用）と月次「構成銘柄別ウエイト一覧」CSV（差分検知用）の解析
- `scripts/seed_topix.py --pdf topix_j.pdf`: `state/topix/{membership,transition,seed_2026}.json` を生成（コミット済み）。
  継続 951 銘柄は `effective_from=2026-04-01`、新規追加 35 銘柄は `2026-10-30`
- `state/topix/transition.json`: 移行措置 683 銘柄。CSV に残っていてもユニバースに入れない（`exclude_until: 2028-07-31`）
- `universe.snapshot_min_as_of: 2026-10-30`: それより古い CSV（旧 TOPIX）は適用せず job_runs に `skipped`
- 実行: `python scripts/update_watch.py --universe topix`。GitHub Actions が JPX400 と同じジョブ内で平日4回実行する。
  Linux PC で動かす場合は `deploy/systemd/README.md`（その場合は Actions 側の TOPIX ステップを外して二重実行を避ける）
- 銘柄の随時変更（TOB・合併）は `state/topix/adjustments.json` に手で追記（形式は JPX400 と同じ）

## テスト

```bash
pytest -q
```

EDINET/yfinanceの本番APIをテストから呼ばず、ZIP・書類・価格を合成して検証します。

## データソースと免責

- 開示情報: 金融庁 EDINET API v2
- 株価情報: Yahoo Finance / yfinance（個人・調査目的）
- 市場代理: TOPIX連動ETF（1306）

本ツールは情報整理用で、投資判断を推奨しません。情報の正確性・完全性を保証せず、
投資判断は利用者自身の責任で行ってください。
