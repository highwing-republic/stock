# JPX400 Watch Radar（修正版MVP-R1）実装仕様

元計画: `Codex向け実装計画書｜JPX400 Event-Driven Investment Radar MVP.md`（2026-09-14）。
同日にユーザー承認された修正点を反映した、実装用の確定仕様。

## 0. 前提と修正点

- 既存の v1 パイプライン（`src/radar.py`、`scripts/update.py`、`public/data/{latest,signals,metadata}.json`、
  `public/data/stocks/*.json`）は**変更しない**。現行ページはそのまま動き続ける。
- 新機能は `src/watch/` パッケージと `scripts/update_watch.py` に追加し、出力は `public/data/watch/` に分離する。
- **TDnet は取得しない**（JPX が無料閲覧サービスの自動取得を控えるよう明記）。決算イベントは対象外。
  `EventSource` インターフェースだけ用意し、将来の正規手段に差し替えられるようにする。
- 信用残は補助情報。取得・検証に成功したときだけ使う（`margin.enabled` 設定＋実行時検証）。
  Watch 入りやランキングの必須条件にしない。
- ベンチマークは当面 `1306.T`（TOPIX連動ETF）。UI では「TOPIX連動ETF（1306）を代理指標として使用」と表記する。
- TradingView は詳細画面の**人間確認用ウィジェットのみ**。判定には使わない。帰属表示は消さない。
- 予測・推奨・スコアの数値は UI／公開 JSON に出さない。

## 1. ディレクトリ

```text
config/watch.yaml            閾値（すべてここ。コードに直書きしない）
src/watch/
  __init__.py
  config.py                  YAML 読み込み＋既定値＋検証
  calendar.py                営業日・event_market_date
  universe.py                JPX400 membership 読み書き・期間判定
  sources/jpx400.py          JPX 公式ファイル取得・解析（担当 A）
  sources/margin.py          信用残 PDF 取得・解析（担当 A）
  events.py                  filings → 正規化イベント
  prices.py                  JPX400＋ベンチマークの価格更新・分割検出・調整 OHLC
  indicators.py              MA/ATR/レンジ/相対リターン
  setups.py                  BREAKOUT / RECOVERY / TREND 判定と Setup 履歴
  episodes.py                Watch Episode 状態機械（日次リプレイ）
  ranking.py                 Priority Score・週次 TOP5／月次 TOP10・ヒステリシス
  performance.py             Radar 初登場後成績・イベント反応
  reasons.py                 日本語の注目理由生成（最大4件）
  store.py                   state/ の JSON 読み書き（原子的書き込み）
  export.py                  public/data/watch/ 出力（一時ディレクトリ→検証→置換）
  pipeline.py                全体オーケストレーション・品質ゲート・job_runs
scripts/update_watch.py      CLI
state/                       永続化する正本（Git 管理。Actions がコミット）
public/data/watch/           公開 JSON
tests/watch/                 テスト
```

依存追加: `pypdf`, `PyYAML`。

## 2. 永続化

SQLite（`data/investment_radar.db`、Actions cache）は**再構築可能なキャッシュ**。履歴の正本は `state/`。

| パス | 内容 |
|---|---|
| `state/jpx400_membership.json` | `{"schema_version":1,"last_checked_at","last_source_sha256","members":[{"security_code","company_name","effective_from","effective_to"(null=現役),"source_url","source_published_at","seeded":bool}]}` |
| `state/events/YYYY-MM.json` | その月の正規化イベント（`event_id` 昇順、再実行で同一内容） |
| `state/episodes.json` | 全 Episode（進行中＋終了）と `episode_events` |
| `state/setup_history/YYYY-MM.json` | JPX400 銘柄の Setup 変化ログ（変化があった日のみ） |
| `state/rankings/weekly/YYYY-Www.json`, `state/rankings/monthly/YYYY-MM.json` | 期間中は毎回上書き、期間終了後は `"final": true` で凍結（以後書き換え禁止） |
| `state/margin/latest.json`, `state/margin/history/YYYY-MM.json` | JPX400 銘柄分のみ |
| `state/job_runs/YYYY-MM.json` | `{source, started_at, finished_at, status(ok/partial/failed/skipped), records, error}` の配列 |

すべての書き込みは一時ファイル→`replace`。JSON は `ensure_ascii=False, indent=1, sort_keys=True` で決定的に出力する。

## 3. JPX400 ユニバース（universe.py / sources/jpx400.py）

- 取得元は JPX 公式の構成銘柄ファイル（担当 A が PoC で確定する）。
- 品質ゲート: 解析件数が `universe.min_members`（既定 350）未満、または証券コード形式不正があれば**更新拒否**。
  前回の membership を維持し、job_runs に `failed`。
- 差分: 新規コード → `effective_from = 公表ファイルの基準日`、消えたコード → `effective_to = 基準日の前営業日`。
- 初回シード: 現行リストを `effective_from = 2026-08-31`（2026年定期入替の適用日）、`seeded: true` で登録。
  可能なら 2026 年定期入替の公表資料から除外銘柄を復元し、`effective_to = 2026-08-28` で登録する。
  シード前の日付でメンバーを判定できない銘柄は「不明」で、非メンバーとして扱う。
- API: `is_member(code, on: date) -> bool`、`members_on(on) -> set[str]`、`all_codes() -> set[str]`。
- 「常に 400 件」を前提にしない。

## 4. 営業日と event_market_date（calendar.py）

- 営業日は、ベンチマーク（1306.T）の価格が存在する日とする。価格データより先の日付は、土日と
  `config.calendar.extra_holidays`（YAML に列挙）を除いた平日で補完する。
- EDINET の `submit_datetime` は JST。
  - 営業日の `市場終了時刻`（既定 15:30）**より前** → 当日が Day0
  - 市場終了時刻以降、または非営業日 → 次の営業日が Day0
- `event_market_date` は、filings から毎回決定的に再計算する（まだ価格が無い未来日の場合もある）。

## 5. 正規化イベント（events.py）

入力は SQLite の `filings`。**書類単位で1イベント**、`event_id = "edinet:{doc_id}"`（重複排除キー）。

```text
event_id, security_code, company_name, filer_name, event_type, tags[], disclosed_at,
event_market_date, source="EDINET", source_document_id, source_url, raw_title,
holding_ratio, previous_holding_ratio, holding_change, important_proposal, is_jpx400_at_event
```

- `source_url`: `https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}` 形式
  （v1 の詳細ページと同じ規則があればそれに合わせる）。
- event_type の優先順:
  1. `report_type == "correction"` → `CORRECTION`（トリガーにしない。タイムライン表示のみ）
  2. `report_type == "initial"` → `NEW_HOLDER`
  3. `holding_change` が None → `CHANGE_UNKNOWN`（トリガーにしない）
  4. `holding_change < 0` → `DECREASE`
  5. 同一提出者×同一銘柄で、`events.consecutive_window_days`（既定 30 暦日）以内に
     2 件目以降の増加（`holding_change > 0`） → `CONSECUTIVE_INCREASE`
  6. `holding_change >= events.large_increase_pt`（既定 1.0） → `LARGE_INCREASE`
  7. `holding_change > 0` → `INCREASE`
- tags: 該当するものをすべて付ける（例 `["LARGE_INCREASE","CONSECUTIVE_INCREASE","PURPOSE_CHANGE"]`）。
  `important_proposal == 1` なら `PURPOSE_CHANGE` タグを付ける。
- トリガーになる event_type: `events.trigger_types`（既定 `NEW_HOLDER, LARGE_INCREASE, CONSECUTIVE_INCREASE`）。
  `INCREASE` は、既存 Episode の強化材料として使う。
- 月別ファイルへの保存は、対象期間（`pipeline.replay_days`、既定 180 日）の全イベントを毎回再生成する。
  同じ入力なら同じバイト列になる（冪等性）。

## 6. 価格（prices.py）

- 対象ティッカー: JPX400 の全 `all_codes()`（期間中に一度でもメンバーだった銘柄）と `1306.T`。
- 既存の `prices` テーブルを共用する（v1 と同じ列）。
- 履歴長: 最低 `prices.history_days`（既定 560 暦日）。不足している銘柄は全期間を取得する。
- 取得は `yf.download(tickers=[...], group_by="ticker", auto_adjust=False, actions=False, threads=True)` で
  まとめて行う（チャンク 50 件）。失敗したチャンクは 1 件ずつ再試行する。
- **分割検出**: 取得データと保存済みデータの重複日で、`close` が `prices.split_mismatch_pct`
  （既定 2%）以上ずれた銘柄は、全履歴を取り直して置き換える。
- 調整 OHLC: `f = adj_close / close` として、`o*f, h*f, l*f, adj_close` を使う。すべての指標は
  この同一基準で計算する。出来高は生値。
- 品質ゲート: 最新営業日（ベンチマークの最終日）に価格がある JPX400 銘柄の割合が
  `prices.min_coverage`（既定 0.8）未満なら、Setup・Episode・ランキングの更新を止める。
  この場合、前回の公開 JSON と state は変更しない。job_runs には `failed` を記録する。

## 7. 指標（indicators.py）— 銘柄ごとの日次系列

`ma20, ma60, ma200, ma20_slope5 = ma20/ma20.shift(5)-1, ma60_slope10, atr20`
（TR の単純平均、調整 OHLC）、`atr_pct = atr20/close`、`atr_pct_rank252`
（当日値の過去 252 日内パーセンタイル 0–1）、`high60, low60`（調整高値・安値、当日を含む）、
`range_pos60 = (close-low60)/(high60-low60)`（分母 0 のときは None）、`high252`、
`dist_52w_high = 1-close/high252`、`vol_ratio20 = volume / volume の20日平均`（当日を除く）、
`ret5 = close/close.shift(5)-1`、`rel20 = ret20(stock) - ret20(bench)`、`rel60` も同様、
`max_drawdown_120 = 直近120日のうち (1 - close/その時点のhigh252) の最大値`、
`days_since_low60 = 直近60日の最安値からの経過営業日数`。

必要な履歴が不足しているとき（例: 200 日未満）は、その日を `INSUFFICIENT_HISTORY` とし、Setup は成立しない。

## 8. Price Setup（setups.py）— config.price_setup.*

- **BREAKOUT**: `close >= ma20` かつ `ma20_slope5 >= 0` かつ `range_pos60 >= 0.70`
  かつ `atr_pct_rank252 <= 0.40` かつ `ret5 < surge_5d_max`（既定 0.15）
- **RECOVERY**: `max_drawdown_120 >= 0.15` かつ `days_since_low60 >= 10` かつ `close >= ma20`
  かつ `ma20_slope5 > 0` かつ `rel20 > rel20.shift(10)`
- **TREND**: `close > ma20 > ma60` かつ `ma60_slope10 > 0` かつ `rel60 > 0`
  かつ `dist_52w_high <= 0.10` かつ `ret5 < surge_5d_max`
- 複数成立したときの主 Setup は `price_setup.priority`（既定 `[BREAKOUT, TREND, RECOVERY]`）の順で決める。
  成立したものはすべて `setups[]` に保持する。
- Setup 履歴: 主 Setup の変化と、次のマイルストーンを記録する。
  `MA20_RECLAIMED`（close が ma20 を下から上抜け）、`MA60_RECLAIMED`、
  `NEW_HIGH60`（close > 前日までの high60）、`REL20_TURNED_POSITIVE`。
  - 「今週 Setup が改善」の判定は、`RECOVERY→TREND/BREAKOUT`、`なし→任意`、またはマイルストーンの発生。

## 9. Watch Episode（episodes.py）

**状態は保存値からの差分更新ではなく、`replay_days` 期間の日次リプレイで毎回決定的に算出する**。
入力は events・membership・prices・config で、同じ入力なら同じ出力になる。
`state/episodes.json` は結果の保存（監査・UI 用）で、リプレイの入力には使わない。

- 1 銘柄につき、進行中の Episode は最大 1 件。
- 開始: 営業日 d に、`is_member(code, event_market_date)` を満たすトリガーイベントがあり、
  その銘柄に進行中の Episode が無ければ、`CANDIDATE` を作る
  （`episode_id = "{code}-{event_market_date}"`、`primary_trigger_event_id`）。
- 進行中の Episode がある銘柄に後続イベントが来た場合は、`episode_events` に追加する
  （CORRECTION・DECREASE も追加する）。
- 日次の遷移（各営業日の終値で評価。1 日に 1 段階まで）:
  - `CANDIDATE → WATCH`: Setup が成立していて、OVEREXTENDED でない
    （TREND は上昇だけを理由に除外しない）。`watch_started_at = d`、
    `initial_price = 調整終値`、`initial_bench = ベンチマーク調整終値` を記録する。
  - `CANDIDATE → CLOSED(NO_SETUP)`: `episodes.candidate_expiry_days`（既定 20 営業日）以内に WATCH にならなかった。
  - `WATCH → STRENGTHENING`: WATCH 開始後に、INCREASE 以上のイベント・Setup 改善・
    `REL20_TURNED_POSITIVE` のいずれかがあった。
  - `WATCH/STRENGTHENING → CONFIRMED`: `NEW_HIGH60` かつ `vol_ratio20 >= confirm_volume_ratio`（既定 1.5）
    かつ `rel20 > 0`。
  - `WATCH/STRENGTHENING/CONFIRMED → REACTED`: WATCH 開始後の騰落率 `>= reacted_return`（既定 0.15）、
    または超過リターン `>= reacted_excess`（既定 0.10）。
  - `→ BROKEN`（WATCH/STRENGTHENING/CONFIRMED から）: Setup 不成立かつ `close < ma20` が
    `broken_days`（既定 3 営業日）連続した、または `close < ma60 * (1 - broken_ma60_buffer)`
    （既定 0.03）。
  - `BROKEN → CLOSED(BROKEN)`: 翌営業日。
  - `→ CLOSED(EXPIRED)`: WATCH 開始から `max_watch_days`（既定 60 営業日）が経過した。
    ただし、期間中に新しいトリガーがあれば、その日から期限を再計算する。
  - `→ CLOSED(UNIVERSE_EXIT)`: JPX400 から除外された。
- OVEREXTENDED: Day0 前日の終値からの騰落率が `overextended_return`（既定 0.20）以上。
  Episode には `overextended: true` を付ける。
- 状態変化の記録: `status_history: [{date, from, to, reason_code}]`。

## 10. Priority Score とランキング（ranking.py）— 内部専用

`score = event(0–35) + setup(0–35) + confirmation(0–15) + improvement(0–10) + margin(0–5) - penalty`

- event: `NEW_HOLDER` 30、`LARGE_INCREASE` 30、`CONSECUTIVE_INCREASE` 35、`INCREASE` 15、
  `PURPOSE_CHANGE` +5（上限 35）。Episode 内の最大値に、Day0 からの経過営業日による減衰
  `max(0.5, 1 - age/60)` を掛ける。
- setup: `BREAKOUT` 30、`TREND` 25、`RECOVERY` 20 に、`range_pos60 >= 0.9` なら +5（上限 35）。
- confirmation: `CONFIRMED` 15、`STRENGTHENING` 8、`REACTED` 5。
- improvement: 直近 5 営業日に Setup 改善またはマイルストーンがあれば 10。
- margin: 信用残データがあり、信用買残が前回比で減少なら 5（データが無ければ 0）。
- penalty: OVEREXTENDED なら 10。
- 対象: `status ∈ {WATCH, STRENGTHENING, CONFIRMED, REACTED}` で、当日 JPX400 メンバーの銘柄。
  REACTED は既存メンバーの維持だけで、新規には入れない。
- **週次 TOP5**（ISO 週 `YYYY-Www`、週の最初の営業日から日次リプレイ）:
  1. 対象外になったメンバー（BROKEN・CLOSED・非メンバー）を除く。
  2. 空いた枠をスコア順に埋める。
  3. 非メンバーの最高スコアが、メンバー最低スコア＋`ranking.weekly_hysteresis`（既定 8）以上なら入れ替える
     （1 日 1 件まで）。
  4. 表示順は当日スコア順。
- **月次 TOP10**: 同じ手順で、ヒステリシスは `ranking.monthly_hysteresis`（既定 8）。
  月次スコアは `score + 3 × 月内の追加 INCREASE 系イベント数 + 5 × 月内 Setup 改善の有無`（上限 110）。
- 保存: `state/rankings/...` に `{period_type, period_key, snapshot_date, final, entries:[{rank, security_code, priority_score, status, setup_type}]}`。
  期間が終わったものは `final:true` で凍結し、以後の実行では書き換えない。

## 11. 成績（performance.py）

- Episode: WATCH 開始時の `initial_price`・`initial_bench` から、+5・+20・+60 営業日時点と現在の
  `stock_return, bench_return, excess_return` を出す（未到達は null。「判定待ち（あと n 営業日）」を出せるよう
  `pending_days` を持つ）。
- イベント: `base = Day0 前営業日の調整終値`、`D1 = Day0 終値`、`D5 = Day0+4`、`D20 = Day0+19` の騰落率と
  ベンチ差（未到達は null）。

## 12. 注目理由（reasons.py）— 最大4件・日本語・数値は短く

例: `新規大量保有 6.4%（野村證券）`、`2pt買い増し 5.1%→7.1%`、`30日以内に2回買い増し`、
`60日レンジ上位12%`、`20日線上向き`、`TOPIX連動ETF比 +4.2pt（20日）`、`52週高値まで6%`、
`信用買残 -14%`、`出来高 1.8倍で60日高値更新`。
予測・推奨表現（「上昇確率」「買い」など）は使わない。テストで禁止語チェックを行う。

## 13. 公開 JSON（export.py）— `public/data/watch/`

一時ディレクトリに全ファイルを書き、スキーマ検証に通ったら置き換える。失敗時は既存ファイルを残す。

`summary.json`

```json
{
  "schemaVersion": 1,
  "updatedAt": "2026-09-29T21:05:00+09:00",
  "asOfMarketDate": "2026-09-29",
  "benchmarkLabel": "TOPIX連動ETF（1306）",
  "sources": {
    "edinet":  {"status": "ok", "lastSuccessAt": "..."},
    "prices":  {"status": "ok", "lastSuccessAt": "...", "coverage": 0.99},
    "jpx400":  {"status": "ok", "lastSuccessAt": "...", "memberCount": 398},
    "margin":  {"status": "disabled|ok|stale|failed", "asOf": null}
  },
  "weekly":  {"periodKey": "2026-W40", "entries": [Entry]},
  "monthly": {"periodKey": "2026-09",  "entries": [Entry]},
  "stateChanges": [{"date", "securityCode", "companyName", "change": "NEW_WATCH|STRENGTHENING|BREAKOUT_CONFIRMED|REACTED|SETUP_BROKEN|CLOSED", "setupType", "reason"}],
  "universeCount": 398,
  "disclaimer": "…"
}
```

`Entry = {rank, securityCode, companyName, setupType, status, reasons[<=4], watchStartedAt, returnSinceWatch, excessSinceWatch, rankChange: "new|up|down|same"}`
（騰落率は % 表記の数値で小数 1 桁。scoreは含めない）

`stocks/{code}.json`（ランキング・状態変化に出た銘柄と、進行中 Episode の銘柄だけ）

```json
{
  "schemaVersion": 1, "updatedAt": "...",
  "company": {"securityCode", "name", "tradingViewSymbol": "TSE:1234"},
  "episode": {"id", "status", "setupType", "setups", "watchStartedAt", "startedAt", "overextended", "statusHistory": [...]},
  "pastEpisodes": [{"id", "startedAt", "closedAt", "closeReason", "setupType"}],
  "reasons": ["..."],
  "timeline": [{"date", "kind": "EVENT|SETUP|STATUS", "label", "detail", "sourceUrl"}],
  "priceSetup": {"asOf", "labels": ["20日線の上", "60日レンジ上位12%"], "ma20", "ma60", "ma200", "rangePos60", "distFrom52wHigh", "atrState": "低ボラ|通常|高ボラ", "rel20", "rel60", "volumeRatio20"},
  "margin": null,
  "performance": {"initialDate", "initialPrice", "currentPrice", "stockReturn", "benchReturn", "excessReturn", "d5": {...}, "d20": {...}, "d60": {...}},
  "eventReactions": [{"eventId", "d1", "d5", "d20", "excess5"}]
}
```

`margin`: `{"asOf", "buyBalance", "sellBalance", "ratio", "buyChangePct", "sellChangePct", "labels": ["信用買残減少"]}`、または null。

## 14. パイプライン（pipeline.py / scripts/update_watch.py）

順序: JPX400 更新確認 → （v1 の update.py で EDINET 取得済み）→ 価格更新 → 信用残 → イベント正規化 →
指標・Setup → Episode リプレイ → ランキング → 成績 → 品質検査 → 一時出力 → 置換 → state 保存 → job_runs。

- 各ソースは独立して失敗できる。JPX400 取得に失敗しても、前回の membership で続行する（`stale`）。
  信用残に失敗しても `failed` のまま本体は続行する。
- 価格の品質ゲートで止まった場合は、公開 JSON と state を変更しない（job_runs だけ記録する）。
- CLI: `python scripts/update_watch.py [--as-of YYYY-MM-DD] [--offline]`
  - `--offline`: ネットワーク取得をせず、DB と state だけで再計算する（テスト・再現用）。
- 同じ日に 2 回実行しても、state と公開 JSON がバイト単位で同一になる（`updatedAt` と job_runs を除く）。

## 15. GitHub Actions

`update-radar.yml` の「Update data」の後に、`python scripts/update_watch.py` を実行するステップを追加する
（`continue-on-error: true`。v2 の失敗で v1 の更新を止めない）。コミット対象に `state` を追加する。
スケジュールは現行の平日 4 回のまま（最終回 18:00 JST で当日終値は確定済み）。
テストは `python -m pytest -q` で全体を実行する。

## 16. 信用残（sources/margin.py）

- 9/28 以降の日次 PDF（前営業日時点）を優先する。取得・解析・件数検証（全銘柄数が
  `margin.min_rows` 以上、かつ JPX400 の `margin.min_universe_coverage` 以上をカバー）に通ったときだけ採用する。
- 失敗時は週次の「銘柄別信用取引週末残高」にフォールバックする。それも失敗した場合は前回のデータを維持し、`stale` とする。
- `margin.enabled` 設定（既定 true）。実行時検証に通らなければ UI 上は `disabled/failed` とする。
- 保存するのは JPX400 銘柄分だけ。

## 17. テスト（tests/watch/）

計画書 §41 に従う。ネットワークには触らない（合成データ／縮小フィクスチャを使う）。
最低限、次を確認する。

- membership の期間判定と非定期除外
- 件数不足での更新拒否
- EDINET の dedupe・訂正除外・NEW_HOLDER・連続買増
- event_market_date（15:29／15:30／週末／祝日）
- 分割データでの誤発火なし
- MA・ATR・相対リターン
- 3 種の Setup と BROKEN
- Episode の新規作成・既存への統合・状態遷移・CLOSED・再開
- TOP5／TOP10・ヒステリシス・凍結スナップショットの不変性
- 2 回実行でのバイト一致
- 失敗時に空データで上書きしないこと
- JPX400 外がランキングに入らないこと（AC-02）
- 公開 JSON に score や禁止語が無いこと

## 18. UI（makoto_consul リポジトリ）

`edinet-investment-radar/watch/index.html`（トップ）と `watch/stock.html?code=XXXX`（詳細）を新設する。
既存ページとナビゲーションからはリンクしない（`noindex`）。データ URL は既定で
`https://raw.githubusercontent.com/highwing-republic/stock/main/public/data/watch`。
`?data=<base>` で上書きできるようにする（ステージング検証用）。

- トップ: 今週の注目 TOP5（カード）、今月の注目 TOP10（コンパクトな一覧）、状態変化、データ更新状況、免責。
  EDINET 全件一覧・件数 KPI・スコアは出さない。
- 詳細: Header（名称・コード・Setup・Status）、注目理由、TradingView Advanced Chart Widget（`TSE:{code}`、
  日足、ローソク足、出来高、6〜12 か月、帰属表示あり、横スクロールなし）、イベントタイムライン、
  Price Setup（状態説明中心）、信用需給（あるときだけ）、Radar 成績。
- スマートフォンで TOP5 が縦に読みやすいこと。
