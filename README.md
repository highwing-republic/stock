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
