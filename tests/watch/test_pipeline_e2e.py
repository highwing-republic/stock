import json
import re
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import JST, run_pipeline
from synth import insert_prices, make_path, price_rows

from src.watch import export
from src.watch.store import Store

FIX = Path(__file__).parent / "fixtures"


def tree(root: Path, skip=("job_runs",)) -> dict:
    out = {}
    for p in sorted(root.rglob("*.json")):
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] in skip:
            continue
        out[rel] = p.read_bytes()
    return out


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_pipeline_publishes_and_is_idempotent(env):
    r1 = run_pipeline(env)
    assert r1.status == "ok" and r1.published, r1.message
    state1, public1 = tree(env.state), tree(env.out)
    r2 = run_pipeline(env)
    assert r2.status == "ok"
    assert tree(env.state) == state1 and tree(env.out) == public1        # 再実行でバイト一致（AC-11）
    # now だけが違う場合: updatedAt / lastSuccessAt 以外は同一（state はバイト一致）
    later = env.now + timedelta(hours=3)
    run_pipeline(env, now=later)
    assert load(env.out / "summary.json")["updatedAt"] == later.isoformat(timespec="seconds")      # AC-12
    state3 = tree(env.state)
    assert {k: v for k, v in state3.items() if k != "jpx400_membership.json"} ==         {k: v for k, v in state1.items() if k != "jpx400_membership.json"}

    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k not in ("updatedAt", "lastSuccessAt")}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj

    public3 = tree(env.out)
    assert public3.keys() == public1.keys()
    for k in public1:
        assert strip(json.loads(public3[k])) == strip(json.loads(public1[k])), k


def test_top5_membership_and_episode_rules(env):
    run_pipeline(env)
    summary = load(env.out / "summary.json")
    members = {"1001", "1002", "1003", "1005", "1006"}
    weekly = [e["securityCode"] for e in summary["weekly"]["entries"]]
    monthly = [e["securityCode"] for e in summary["monthly"]["entries"]]
    assert set(weekly) == {"1001", "1003"}                                 # WATCH 以上のみ（AC-03）
    assert set(weekly + monthly) <= members and "1004" not in weekly + monthly   # JPX400 外は入らない（AC-02）
    assert "1005" not in weekly + monthly and "1006" not in weekly + monthly
    assert len(weekly) <= 5 and len(monthly) <= 10
    assert summary["universeCount"] == 5 and summary["asOfMarketDate"] == env.as_of.isoformat()
    assert summary["weekly"]["periodKey"].startswith("2026-W") and summary["monthly"]["periodKey"] == "2026-09"
    # 複数イベントは 1 Episode に統合（AC-04）、訂正はトリガーにならない
    eps = load(env.state / "episodes.json")["episodes"]
    e1002 = [e for e in eps if e["security_code"] == "1002"]
    assert len(e1002) == 1 and e1002[0]["event_ids"] == ["edinet:S1002A", "edinet:S1002B", "edinet:S1002C"]
    assert e1002[0]["primary_trigger_event_id"] == "edinet:S1002A"
    codes = {e["security_code"] for e in eps}
    assert codes == {"1001", "1002", "1003", "1006"}                        # 1004(非メンバー)/1005(小口) は無し
    e1006 = [e for e in eps if e["security_code"] == "1006"][0]
    assert e1006["status"] == "CLOSED" and e1006["close_reason"] == "NO_SETUP"
    # 分割銘柄は崩れ扱いにならない
    e1003 = [e for e in eps if e["security_code"] == "1003"][0]
    assert not any(h["to"] in ("BROKEN", "CLOSED") for h in e1003["status_history"])
    assert e1003["status"] in ("WATCH", "STRENGTHENING", "CONFIRMED", "REACTED")
    # 週次・月次のスナップショットは state に保存され、終了期間は凍結される
    weekly_files = sorted((env.state / "rankings" / "weekly").glob("*.json"))
    assert weekly_files and json.loads(weekly_files[-1].read_text(encoding="utf-8"))["final"] is False
    assert json.loads(weekly_files[0].read_text(encoding="utf-8"))["final"] is True
    events_file = json.loads((env.state / "events" / "2026-07.json").read_text(encoding="utf-8"))
    assert [e["event_id"] for e in events_file["events"]] == sorted(e["event_id"] for e in events_file["events"])
    # 個別ページ: 履歴・成績・イベント反応
    stock = load(env.out / "stocks" / "1003.json")
    assert stock["company"]["tradingViewSymbol"] == "TSE:1003"
    assert stock["performance"]["initialDate"] == stock["episode"]["watchStartedAt"]
    assert stock["eventReactions"][0]["eventId"] == "edinet:S1003A" and stock["reasons"]
    assert stock["priceSetup"]["asOf"] == env.as_of.isoformat()


def test_frozen_snapshots_survive_rerun_with_changed_inputs(env):
    run_pipeline(env)
    finals = {p: p.read_bytes() for p in (env.state / "rankings").rglob("*.json")
              if json.loads(p.read_text(encoding="utf-8"))["final"]}
    assert finals
    # 入力（価格）を大きく変えて再実行しても、凍結済みスナップショットは書き換わらない
    env.conn.execute("UPDATE prices SET adj_close=adj_close*1.3, close=close*1.3 WHERE ticker='1001.T'")
    env.conn.commit()
    run_pipeline(env)
    for p, before in finals.items():
        assert p.read_bytes() == before


def test_public_json_has_no_scores_or_forbidden_words(env):
    run_pipeline(env)
    for p in env.out.rglob("*.json"):
        text = p.read_text(encoding="utf-8")
        for word in ("上昇確率", "買い推奨", "推奨", "強気度"):
            assert word not in text
        assert not re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", text)
        assert not re.search(r'"[^"]*(score|priority)[^"]*"\s*:', text, re.I)


def test_quality_gate_leaves_public_and_state_untouched(env):
    run_pipeline(env)
    public, state = tree(env.out), tree(env.state)
    # 最新営業日の価格を大半の銘柄で欠損させる（カバレッジ < 80%）
    last = env.as_of.isoformat()
    env.conn.execute("DELETE FROM prices WHERE trade_date=? AND ticker IN ('1001.T','1002.T','1003.T')", (last,))
    env.conn.commit()
    res = run_pipeline(env)
    assert res.status == "failed" and "coverage" in res.message
    assert tree(env.out) == public and tree(env.state) == state          # 空データで上書きしない
    runs = json.loads((env.state / "job_runs" / f"{env.as_of.isoformat()[:7]}.json").read_text(encoding="utf-8"))
    assert runs[-1]["source"] == "watch" and runs[-1]["status"] == "failed"


def test_stale_prices_fail_gate(env):
    run_pipeline(env)
    public = tree(env.out)
    res = run_pipeline(env, as_of=env.as_of + timedelta(days=30))
    assert res.status == "failed" and "stale" in res.message
    assert tree(env.out) == public


def test_empty_universe_does_not_overwrite(env):
    run_pipeline(env)
    public = tree(env.out)
    (env.state / "jpx400_membership.json").write_text("{}", encoding="utf-8")
    res = run_pipeline(env)
    assert res.status == "failed" and "membership" in res.message
    assert tree(env.out) == public


def test_missing_membership_file(env, tmp_path):
    (env.state / "jpx400_membership.json").unlink()
    res = run_pipeline(env)
    assert res.status == "failed" and not env.out.exists()


def test_validation_failure_keeps_existing_public(env, monkeypatch):
    run_pipeline(env)
    public = tree(env.out)
    monkeypatch.setattr(export, "REASON_TEXT", dict(export.REASON_TEXT, SETUP_OK="買い推奨"))
    (env.state / "episodes.json").unlink()
    res = run_pipeline(env)
    assert res.status == "failed" and "forbidden" in res.message
    assert tree(env.out) == public


def test_online_run_uses_injected_fetchers_and_margin_failure_keeps_previous(env):
    """--offline 無しの経路: JPX400 失敗は stale で続行、信用残失敗でも本体は続行して前回データを維持。"""
    def boom_jpx():
        raise RuntimeError("jpx down")

    def boom_margin(**kw):
        raise RuntimeError("margin down")

    def no_download(tickers, start, end):
        return {}

    Store(env.state).write_margin({"schema_version": 1, "as_of": "2026-09-22", "kind": "daily", "source_url": "u",
                                   "count": 1},
                                  {"1001": [{"as_of": "2026-09-14", "kind": "daily", "buy_balance": 1000,
                                             "sell_balance": 100},
                                            {"as_of": "2026-09-22", "kind": "daily", "buy_balance": 800,
                                             "sell_balance": 100}]})
    before = tree(env.state / "margin")
    res = run_pipeline(env, offline=False, jpx_fetcher=boom_jpx, margin_fetcher=boom_margin, downloader=no_download)
    assert res.status == "ok", res.message
    summary = load(env.out / "summary.json")
    assert summary["sources"]["jpx400"]["status"] == "stale"
    assert summary["sources"]["margin"] == {"status": "stale", "asOf": "2026-09-22"}     # AC-10
    assert tree(env.state / "margin") == before
    stock = load(env.out / "stocks" / "1001.json")
    assert stock["margin"]["buyChangePct"] == -20.0 and stock["margin"]["asOf"] == "2026-09-22"
    assert "信用買残減少" in stock["margin"]["labels"]
    runs = json.loads((env.state / "job_runs" / f"{env.as_of.isoformat()[:7]}.json").read_text(encoding="utf-8"))
    assert {r["source"]: r["status"] for r in runs} == {"jpx400": "failed", "prices": "ok", "margin": "failed",
                                                        "watch": "ok"}


# ---------------------------------------------------------------- UI サンプルとのキー構造照合
def keys_of(obj):
    return set(obj)


def test_export_matches_ui_sample_shape(env):
    run_pipeline(env)
    sample = load(FIX / "ui_sample_summary.json")
    mine = load(env.out / "summary.json")
    assert keys_of(mine) == keys_of(sample)
    assert keys_of(mine["sources"]) == keys_of(sample["sources"])
    for src in sample["sources"]:
        assert set(mine["sources"][src]) == set(sample["sources"][src]), src
    assert keys_of(mine["weekly"]) == keys_of(sample["weekly"])
    assert keys_of(mine["weekly"]["entries"][0]) == keys_of(sample["weekly"]["entries"][0])
    assert keys_of(mine["monthly"]["entries"][0]) == keys_of(sample["monthly"]["entries"][0])
    assert mine["stateChanges"] and keys_of(mine["stateChanges"][0]) == keys_of(sample["stateChanges"][0])
    s_stock = load(FIX / "ui_sample_stock.json")
    stock = load(env.out / "stocks" / "1003.json")
    assert keys_of(stock) == keys_of(s_stock)
    assert keys_of(stock["company"]) == keys_of(s_stock["company"])
    assert keys_of(s_stock["episode"]) <= keys_of(stock["episode"])           # 追加キー(closedAt/closeReason)のみ許容
    assert keys_of(stock["episode"]) - keys_of(s_stock["episode"]) == {"closedAt", "closeReason"}
    assert keys_of(stock["episode"]["statusHistory"][0]) == keys_of(s_stock["episode"]["statusHistory"][0])
    assert keys_of(stock["priceSetup"]) == keys_of(s_stock["priceSetup"])
    assert keys_of(stock["performance"]) == keys_of(s_stock["performance"])
    for h in ("d5", "d20", "d60"):
        assert keys_of(stock["performance"][h]) == keys_of(s_stock["performance"][h])
    assert keys_of(stock["timeline"][0]) == keys_of(s_stock["timeline"][0])
    assert keys_of(stock["eventReactions"][0]) == keys_of(s_stock["eventReactions"][0])
    assert {t["kind"] for t in stock["timeline"]} <= {"EVENT", "SETUP", "STATUS"}
    for e in mine["weekly"]["entries"]:
        assert isinstance(e["rank"], int) and len(e["reasons"]) <= 4
    export.validate_public({"summary.json": mine, **{f"stocks/{p.stem}.json": load(p)
                                                      for p in (env.out / "stocks").glob("*.json")}})


def test_validate_public_rejects_bad_files():
    good = {"schemaVersion": 1, "updatedAt": "x", "asOfMarketDate": "d", "benchmarkLabel": "b",
            "sources": {"edinet": {}, "prices": {}, "jpx400": {}, "margin": {}},
            "weekly": {"periodKey": "k", "entries": []}, "monthly": {"periodKey": "k", "entries": []},
            "stateChanges": [], "universeCount": 0, "disclaimer": "d"}
    export.validate_public({"summary.json": good})
    with pytest.raises(export.ExportValidationError):
        export.validate_public({"summary.json": dict(good, priorityScore=1)})
    with pytest.raises(export.ExportValidationError):
        export.validate_public({"summary.json": dict(good, disclaimer="これは買い推奨ではない")})
    with pytest.raises(export.ExportValidationError):
        export.validate_public({"summary.json": dict(good, disclaimer="AI が選定")})
    bad = dict(good)
    bad.pop("updatedAt")
    with pytest.raises(export.ExportValidationError):
        export.validate_public({"summary.json": bad})
    entry = {"rank": 1, "securityCode": "1", "companyName": "n", "setupType": None, "status": "WATCH", "reasons": [],
             "watchStartedAt": None, "returnSinceWatch": None, "excessSinceWatch": None, "rankChange": "new",
             "score": 3}
    with pytest.raises(export.ExportValidationError):
        export.validate_public({"summary.json": dict(good, weekly={"periodKey": "k", "entries": [entry]})})


def test_publish_replaces_atomically_and_keeps_old_on_failure(tmp_path):
    out = tmp_path / "public" / "watch"
    out.mkdir(parents=True)
    (out / "summary.json").write_text('{"old": true}', encoding="utf-8")
    with pytest.raises(export.ExportValidationError):
        export.publish({"summary.json": {"schemaVersion": 1}}, out)
    assert json.loads((out / "summary.json").read_text(encoding="utf-8")) == {"old": True}
    assert not (tmp_path / "public" / ".watch.tmp").exists()


# ---------------------------------------------------------------- JPX400 更新の統合（snapshot → adjustments）
def test_adjustments_applied_offline_and_idempotent(env):
    d = lambda i: env.days[i].isoformat()  # noqa: E731
    adj = {"schema_version": 1, "adjustments": [
        {"security_code": "1004", "company_name": "フォー商事", "action": "add", "effective_date": d(200),
         "announced_on": d(190), "source_url": "https://example.test/notice"}]}
    (env.state / "jpx400_adjustments.json").write_text(json.dumps(adj), encoding="utf-8")
    assert run_pipeline(env).status == "ok"
    m = load(env.state / "jpx400_membership.json")
    assert any(x["security_code"] == "1004" and x["effective_from"] == d(200) for x in m["members"])
    summary = load(env.out / "summary.json")
    assert summary["universeCount"] == 6                                   # 400 固定を仮定しない
    eps = load(env.state / "episodes.json")["episodes"]
    assert any(e["security_code"] == "1004" for e in eps)                  # 追加後は 1004 も Episode 対象
    before = tree(env.state), tree(env.out)
    assert run_pipeline(env).status == "ok"
    assert (tree(env.state), tree(env.out)) == before


def test_bad_adjustments_file_does_not_stop_run(env):
    (env.state / "jpx400_adjustments.json").write_text('{"schema_version": 9}', encoding="utf-8")
    assert run_pipeline(env).status == "ok"


def test_online_snapshot_then_adjustments_and_rejected_snapshot(env):
    from src.watch.config import load_config
    from src.watch.sources.jpx400 import ConstituentsSnapshot
    cfg = load_config(overrides={"universe": {"min_members": 3}})
    rows = tuple((c, f"銘柄{c}") for c in ("1001", "1002", "1003", "1005", "1006"))
    good = ConstituentsSnapshot(as_of=env.as_of, source_url="u", published_at=None, rows=rows, sha256="abc")
    res = run_pipeline(env, cfg=cfg, offline=False, jpx_fetcher=lambda: good, downloader=lambda t, s, e: {},
                       margin_fetcher=lambda **kw: (_ for _ in ()).throw(RuntimeError("no margin")))
    assert res.status == "ok"
    summary = load(env.out / "summary.json")
    assert summary["sources"]["jpx400"]["status"] == "ok"
    assert load(env.state / "jpx400_membership.json")["last_as_of"] == env.as_of.isoformat()
    # 件数不足のスナップショットは拒否され、前回 membership で続行（stale）
    small = ConstituentsSnapshot(as_of=env.as_of, source_url="u", published_at=None, rows=rows[:2], sha256="def")
    before = (env.state / "jpx400_membership.json").read_bytes()
    res = run_pipeline(env, cfg=cfg, offline=False, jpx_fetcher=lambda: small, downloader=lambda t, s, e: {},
                       margin_fetcher=lambda **kw: (_ for _ in ()).throw(RuntimeError("no margin")))
    assert res.status == "ok"
    assert load(env.out / "summary.json")["sources"]["jpx400"]["status"] == "stale"
    assert (env.state / "jpx400_membership.json").read_bytes() == before


def test_cli_offline(env, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("update_watch", Path(__file__).parents[2] / "scripts" / "update_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    code = mod.main(["--offline", "--as-of", env.as_of.isoformat(), "--db", str(env.db_path),
                     "--state-dir", str(env.state), "--out-dir", str(env.out)])
    assert code == 0 and (env.out / "summary.json").exists()
    (env.state / "jpx400_membership.json").write_text("{}", encoding="utf-8")
    assert mod.main(["--offline", "--as-of", env.as_of.isoformat(), "--db", str(env.db_path),
                     "--state-dir", str(env.state), "--out-dir", str(env.out)]) == 1
