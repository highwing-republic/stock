"""TOPIX ソース（選定結果 PDF テキスト・月次ウエイト CSV）、シード、除外リスト、Store パスのテスト。"""
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.watch.sources import topix
from src.watch.sources.jpx400 import ConstituentsSnapshot
from src.watch.store import Store
from src.watch.universe import Universe, exclude_codes, load_transition

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parents[2]
JST = timezone(timedelta(hours=9))


def selection_text():
    return (FIX / "topix_selection_20261007.txt").read_text(encoding="utf-8")


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- 選定結果 PDF（テキスト）
def test_parse_selection_sections_and_dates():
    sel = topix.parse_selection_text(selection_text(), source_url="u", sha256="h")
    assert sel.published_at == date(2026, 10, 7)
    assert sel.effective_at == date(2026, 10, 30)
    assert [c for c, _, _ in sel.added] == ["1723", "1965", "2160"]
    assert [c for c, _, _ in sel.transition] == ["1301", "1375", "1376", "1379"]
    assert len(sel.constituents) == 11
    names = {c: n for c, n, _ in sel.constituents}
    assert names["141A"] == "トライアルホールディングス"
    assert names["9983"] == "ファーストリテイリング"
    markets = {c: m for c, _, m in sel.constituents}
    assert markets["2160"] == "グロース" and markets["1376"] == "スタンダード"
    # 新 TOPIX = 構成 − 移行措置、継続 = それから新規追加も除いたもの
    assert [c for c, _, _ in sel.members()] == ["1332", "1723", "1965", "2160", "9983", "141A", "646A"]
    assert [c for c, _, _ in sel.continuing()] == ["1332", "9983", "141A", "646A"]


def test_selection_sequence_break_is_error():
    text = selection_text().replace("   2          1965", "   3          1965")
    with pytest.raises(ValueError, match="連番"):
        topix.parse_selection_text(text)


def test_selection_added_must_be_in_constituents():
    text = selection_text().replace("   6          1723      日本電技", "   6          1724      日本電技")
    with pytest.raises(ValueError):
        topix.parse_selection_text(text)


def test_selection_missing_published_date():
    with pytest.raises(ValueError, match="公表日"):
        topix.parse_selection_text(selection_text().replace("2026年10月7日公表", ""))


# --------------------------------------------------------------------------- 月次ウエイト CSV
def test_parse_weight_csv_cp932():
    snap = topix.parse_weight_csv((FIX / "topix_weight_20261030.csv").read_bytes(), source_url="c")
    assert snap.as_of == date(2026, 10, 30)
    assert snap.published_at == "2026-10-30"
    assert [c for c, _ in snap.rows] == ["1301", "1332", "1375", "141A", "1723", "9983"]  # 文字列順
    assert dict(snap.rows)["141A"] == "トライアルホールディングス"
    assert len(snap.sha256) == 64


def test_parse_weight_csv_rejects_mixed_dates_and_missing_columns():
    raw = (FIX / "topix_weight_20261030.csv").read_bytes().decode("cp932")
    mixed = raw.replace("20261030,ニッスイ", "20261031,ニッスイ").encode("cp932")
    with pytest.raises(ValueError, match="複数の日付"):
        topix.parse_weight_csv(mixed)
    with pytest.raises(ValueError, match="列が不足"):
        topix.parse_weight_csv("日付,銘柄名\r\n20261030,x\r\n".encode("cp932"))


# --------------------------------------------------------------------------- 除外リスト
def test_exclude_codes_and_transition_file(tmp_path):
    snap = topix.parse_weight_csv((FIX / "topix_weight_20261030.csv").read_bytes())
    path = tmp_path / "transition.json"
    path.write_text(json.dumps({"schema_version": 1, "exclude_until": "2028-07-31",
                                "codes": [{"security_code": "1301"}, "1375", "１３７６"]}), encoding="utf-8")
    data = load_transition(path, today=date(2026, 11, 1))
    assert data["codes"] == ["1301", "1375", "1376"] and data["expired"] is False
    assert load_transition(path, today=date(2028, 8, 1))["expired"] is True
    out = exclude_codes(snap, set(data["codes"]))
    assert [c for c, _ in out.rows] == ["1332", "141A", "1723", "9983"]
    assert out.as_of == snap.as_of and out.sha256 == snap.sha256
    assert load_transition(tmp_path / "none.json") == {"codes": [], "expired": False}
    path.write_text(json.dumps({"schema_version": 2, "codes": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_transition(path)


# --------------------------------------------------------------------------- Store のパス
def test_store_paths_by_universe(tmp_path):
    assert Store(tmp_path).membership_path == tmp_path / "jpx400_membership.json"
    assert Store(tmp_path, "jpx400").adjustments_path == tmp_path / "jpx400_adjustments.json"
    assert Store(tmp_path, "topix").membership_path == tmp_path / "membership.json"
    assert Store(tmp_path, "topix").adjustments_path == tmp_path / "adjustments.json"


# --------------------------------------------------------------------------- 設定
def test_topix_config_overrides_and_defaults():
    from src.watch.config import load_config
    base = load_config()
    assert (base.universe.name, base.universe.label, base.universe.source) == ("jpx400", "JPX400", "jpx400")
    assert base.universe.transition_file is None and base.universe.snapshot_min_as_of is None
    cfg = load_config(ROOT / "config" / "watch-topix.yaml")
    assert (cfg.universe.name, cfg.universe.label, cfg.universe.source) == ("topix", "TOPIX", "topix")
    assert cfg.universe.min_members == 900 and cfg.universe.transition_file == "transition.json"
    assert str(cfg.universe.snapshot_min_as_of) == "2026-10-30"
    assert cfg.ranking.weekly_size == 10 and cfg.ranking.monthly_size == 20
    # 閾値は JPX400 を継承
    assert cfg.price_setup.breakout.range_pos60_min == base.price_setup.breakout.range_pos60_min
    assert cfg.episodes.max_watch_days == base.episodes.max_watch_days
    with pytest.raises(Exception):
        load_config(overrides={"universe": {"source": "nasdaq"}})


# --------------------------------------------------------------------------- シード
def test_seed_builds_membership_and_transition():
    seed = _load_script("seed_topix")
    sel = topix.parse_selection_text(selection_text(), source_url="u", sha256="h")
    now = datetime(2026, 10, 10, 23, 0, tzinfo=JST)
    membership = seed.build_membership(sel, continuing_from=date(2026, 4, 1), added_from=date(2026, 10, 30),
                                       checked_at=now)
    by_code = {m["security_code"]: m for m in membership["members"]}
    assert set(by_code) == {"1332", "1723", "1965", "2160", "9983", "141A", "646A"}
    assert by_code["1723"]["effective_from"] == "2026-10-30"   # 新規追加
    assert by_code["1332"]["effective_from"] == "2026-04-01"   # 継続
    assert all(m["seeded"] and m["effective_to"] is None for m in membership["members"])
    assert membership["last_as_of"] == "2026-10-07" and membership["last_source_sha256"] == "h"
    uni = Universe(membership)
    assert uni.is_member("1332", date(2026, 6, 1)) and not uni.is_member("1723", date(2026, 10, 29))
    assert uni.is_member("1723", date(2026, 10, 30)) and not uni.is_member("1301", date(2026, 11, 2))
    trans = seed.build_transition(sel)
    assert [c["security_code"] for c in trans["codes"]] == ["1301", "1375", "1376", "1379"]
    assert trans["exclude_until"] == "2028-07-31"


def test_committed_topix_state_is_consistent():
    """リポジトリにコミットした state/topix/ が設計どおり（986 / 683、移行措置と重複なし）であること。"""
    state = ROOT / "state" / "topix"
    if not (state / "membership.json").exists():
        pytest.skip("state/topix not seeded")
    membership = json.loads((state / "membership.json").read_text(encoding="utf-8"))
    trans = load_transition(state / "transition.json", today=date(2026, 11, 1))
    codes = [m["security_code"] for m in membership["members"]]
    assert len(codes) == 986 and len(set(codes)) == 986
    assert len(trans["codes"]) == 683 and not (set(codes) & set(trans["codes"]))
    froms = {m["effective_from"] for m in membership["members"]}
    assert froms == {"2026-04-01", "2026-10-30"}
    assert sum(1 for m in membership["members"] if m["effective_from"] == "2026-10-30") == 35


# --------------------------------------------------------------------------- パイプライン（合成 DB）
def _topix_cfg(env, **universe):
    from src.watch.config import load_config
    over = {"universe": {"name": "topix", "label": "TOPIX", "source": "topix", "min_members": 3,
                         "transition_file": "transition.json", "snapshot_min_as_of": "2026-10-30"}}
    over["universe"].update(universe)
    return load_config(overrides=over)


def _write_topix_state(env):
    from src.watch.store import write_json
    state = env.state.parent / "state_topix"
    write_json(state / "membership.json", env.membership)
    write_json(state / "transition.json", {"schema_version": 1, "exclude_until": "2028-07-31",
                                           "codes": ["1004", "1007"]})
    return state


def test_topix_pipeline_offline_uses_generic_file_names(env):
    from test_pipeline_e2e import load, run_pipeline
    state = _write_topix_state(env)
    out = env.out.parent / "watch-topix"
    res = run_pipeline(env, cfg=_topix_cfg(env), state_dir=state, out_dir=out)
    assert res.status == "ok", res.message
    summary = load(out / "summary.json")
    assert "topix" in summary["sources"] and "jpx400" not in summary["sources"]
    assert summary["universeCount"] == 5
    runs = load(state / "job_runs" / f"{env.as_of.isoformat()[:7]}.json")
    assert {r["source"] for r in runs} == {"watch"}
    assert not (state / "jpx400_membership.json").exists()


def test_topix_online_skips_old_snapshot_and_excludes_transition(env):
    from test_pipeline_e2e import load, run_pipeline
    state = _write_topix_state(env)
    out = env.out.parent / "watch-topix"
    no_margin = lambda **kw: (_ for _ in ()).throw(RuntimeError("no margin"))  # noqa: E731
    rows = tuple((c, f"銘柄{c}") for c in ("1001", "1002", "1003", "1004", "1005", "1006", "1007"))
    # 旧 TOPIX（snapshot_min_as_of より前の基準日）は適用しない。合成 DB の as_of に合わせて下限を 09-20 にする
    cfg = _topix_cfg(env, snapshot_min_as_of="2026-09-20")
    old = ConstituentsSnapshot(as_of=date(2026, 8, 31), source_url="c", published_at="2026-08-31", rows=rows,
                               sha256="old")
    res = run_pipeline(env, cfg=cfg, state_dir=state, out_dir=out, offline=False,
                       jpx_fetcher=lambda: old, downloader=lambda t, s, e: {}, margin_fetcher=no_margin)
    assert res.status == "ok", res.message
    membership = load(state / "membership.json")
    assert membership["last_as_of"] == env.membership["last_as_of"]  # 未適用
    assert len(membership["members"]) == len(env.membership["members"])
    runs = load(state / "job_runs" / f"{env.as_of.isoformat()[:7]}.json")
    topix_runs = [r for r in runs if r["source"] == "topix"]
    assert topix_runs[-1]["status"] == "skipped" and "snapshot_min_as_of" in topix_runs[-1]["error"]
    assert load(out / "summary.json")["sources"]["topix"]["status"] == "ok"  # 保存済み membership は新しい

    # 新基準日の CSV は適用され、移行措置 1004/1007 は除外されたまま、1004 は追加されない
    new = ConstituentsSnapshot(as_of=env.as_of, source_url="c", published_at=env.as_of.isoformat(), rows=rows,
                               sha256="new")
    res = run_pipeline(env, cfg=cfg, state_dir=state, out_dir=out, offline=False,
                       jpx_fetcher=lambda: new, downloader=lambda t, s, e: {}, margin_fetcher=no_margin)
    assert res.status == "ok", res.message
    membership = load(state / "membership.json")
    active = {m["security_code"] for m in membership["members"] if m["effective_to"] is None}
    assert active == {"1001", "1002", "1003", "1005", "1006"}
    assert membership["last_as_of"] == env.as_of.isoformat()
    runs = load(state / "job_runs" / f"{env.as_of.isoformat()[:7]}.json")
    assert [r for r in runs if r["source"] == "topix"][-1]["status"] == "ok"


def test_cli_universe_defaults_and_mismatch(env, tmp_path):
    mod = _load_script("update_watch")
    paths = mod.resolve_paths("topix")
    assert paths["config"].endswith("config/watch-topix.yaml")
    assert paths["state_dir"] == ROOT / "state" / "topix" and paths["out_dir"] == ROOT / "public/data/watch-topix"
    paths = mod.resolve_paths("jpx400")
    assert paths["config"] is None and paths["state_dir"] == ROOT / "state"
    # --universe と設定の universe.name が食い違えば終了コード 2（argparse error）
    with pytest.raises(SystemExit):
        mod.main(["--universe", "topix", "--config", str(ROOT / "config" / "watch.yaml"), "--offline",
                  "--db", str(env.db_path), "--state-dir", str(tmp_path), "--out-dir", str(tmp_path / "o")])
