"""kind=topix で一式を回す e2e。JPX400 版と同じ合成 DB を使い、state/out だけ別ディレクトリにする。"""
import json
from datetime import date

from conftest import run_pipeline
from synth import membership

from src.watch.config import load_config
from src.watch.sources.common import ConstituentsSnapshot
from src.watch.store import write_json


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def topix_env(env, tmp_path, include_transition=False):
    """1001/1002/1003 を継続採用、1005 を 10 月末からの新規、1006 を移行措置（除外）にする。"""
    env.cfg = load_config("config/watch-topix.yaml",
                          overrides={"universe": {"min_members": 2, "include_transition": include_transition}})
    env.state = tmp_path / "state-topix"
    env.out = tmp_path / "public" / "watch-topix"
    m = membership({"1001": "ワン工業", "1002": "ツー化学", "1003": "スリー電機"}, effective_from="2026-01-05",
                   checked=env.now.isoformat(timespec="seconds"),
                   extra=[{"security_code": "1005", "company_name": "ファイブ建設", "effective_from": "2026-10-30",
                           "effective_to": None, "source_url": "test", "source_published_at": None, "seeded": False}])
    m["excluded_codes"] = [] if include_transition else ["1006"]
    write_json(env.state / "topix_membership.json", m)
    return env


def test_topix_pipeline_offline_publishes_with_universe_block(env, tmp_path):
    topix_env(env, tmp_path)
    res = run_pipeline(env)
    assert res.status == "ok" and res.published, res.message
    summary = load(env.out / "summary.json")
    assert summary["universe"] == {"key": "topix", "label": "新TOPIX", "memberCount": 3, "note": "移行措置銘柄を除く"}
    assert set(summary["sources"]) == {"edinet", "prices", "topix", "margin"}
    assert summary["sources"]["topix"]["memberCount"] == 3 and summary["universeCount"] == 3
    codes = {e["securityCode"] for e in summary["weekly"]["entries"] + summary["monthly"]["entries"]}
    assert codes <= {"1001", "1002", "1003"}                      # 新規(10/30 前)・移行措置・非メンバーは出ない
    eps = load(env.state / "episodes.json")["episodes"]
    assert {e["security_code"] for e in eps} <= {"1001", "1002", "1003"}
    assert (env.state / "topix_membership.json").exists() and not (env.state / "jpx400_membership.json").exists()
    runs = load(env.state / "job_runs" / f"{env.as_of.isoformat()[:7]}.json")
    assert runs[-1]["source"] == "watch" and runs[-1]["status"] == "ok"
    # JPX400 版の state/out には触れていない（env フィクスチャが置いた membership だけが残る）
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == ["jpx400_membership.json"]
    assert not (tmp_path / "public" / "watch").exists()


def test_topix_online_snapshot_excludes_transition_and_add_adjustment_reinstates(env, tmp_path):
    topix_env(env, tmp_path)
    as_of = env.as_of
    rows = tuple((c, f"銘柄{c}") for c in ("1001", "1002", "1003", "1006"))     # CSV には移行措置 1006 が残っている
    snap = ConstituentsSnapshot(as_of=as_of, source_url="csv", published_at=as_of.isoformat(), rows=rows, sha256="c1")
    res = run_pipeline(env, offline=False, jpx_fetcher=lambda: snap, downloader=lambda t, s, e: {},
                       margin_fetcher=lambda **kw: (_ for _ in ()).throw(RuntimeError("no margin")))
    assert res.status == "ok", res.message
    m = load(env.state / "topix_membership.json")
    codes_active = {x["security_code"] for x in m["members"] if x["effective_to"] is None}
    assert "1006" not in codes_active and m["excluded_codes"] == ["1006"]
    assert "1005" not in codes_active                                   # CSV に無い新規銘柄は as_of 前営業日で終了扱い
    summary = load(env.out / "summary.json")
    assert summary["sources"]["topix"]["status"] == "ok"
    runs = load(env.state / "job_runs" / f"{as_of.isoformat()[:7]}.json")
    assert [r["source"] for r in runs if r["source"] == "topix"] == ["topix"]
    # 再評価で復帰: adjustments に add を書くと除外対象から外れて CSV から追加される
    adj = {"schema_version": 1, "adjustments": [
        {"security_code": "1006", "company_name": "シックス食品", "action": "add", "effective_date": as_of.isoformat(),
         "announced_on": as_of.isoformat(), "source_url": "https://www.jpx.co.jp/x"}]}
    (env.state / "topix_adjustments.json").write_text(json.dumps(adj), encoding="utf-8")
    snap2 = ConstituentsSnapshot(as_of=as_of, source_url="csv", published_at=as_of.isoformat(), rows=rows, sha256="c2")
    res = run_pipeline(env, offline=False, jpx_fetcher=lambda: snap2, downloader=lambda t, s, e: {},
                       margin_fetcher=lambda **kw: (_ for _ in ()).throw(RuntimeError("no margin")))
    assert res.status == "ok", res.message
    m = load(env.state / "topix_membership.json")
    rec = [x for x in m["members"] if x["security_code"] == "1006"]
    assert rec and rec[-1]["effective_to"] is None and rec[-1]["effective_from"] == as_of.isoformat()


def test_topix_universe_exit_text_uses_label(env, tmp_path):
    topix_env(env, tmp_path)
    # 1003 を as_of の 3 営業日前に除外して UNIVERSE_EXIT を起こす
    m = load(env.state / "topix_membership.json")
    for x in m["members"]:
        if x["security_code"] == "1003":
            x["effective_to"] = env.days[-4].isoformat()
    write_json(env.state / "topix_membership.json", m)
    assert run_pipeline(env).status == "ok"
    changes = load(env.out / "summary.json")["stateChanges"]
    exits = [c for c in changes if c["securityCode"] == "1003" and c["change"] == "CLOSED"]
    assert exits and exits[0]["reason"] == "新TOPIXから除外された"
    text = (env.out / "summary.json").read_text(encoding="utf-8")
    assert "JPX400" not in text and "JPX日経400" not in text


def test_topix_include_transition_true_keeps_all(env, tmp_path):
    topix_env(env, tmp_path, include_transition=True)
    m = load(env.state / "topix_membership.json")
    m["members"].append({"security_code": "1006", "company_name": "シックス食品", "effective_from": "2026-01-05",
                         "effective_to": None, "source_url": "test", "source_published_at": None, "seeded": True})
    write_json(env.state / "topix_membership.json", m)
    assert run_pipeline(env).status == "ok"
    summary = load(env.out / "summary.json")
    assert summary["universe"]["note"] is None and summary["universeCount"] == 4
    assert any(e["security_code"] == "1006" for e in load(env.state / "episodes.json")["episodes"])


def test_jpx400_default_config_unchanged(cfg):
    assert cfg.universe.kind == "jpx400" and cfg.universe.label == "JPX日経400"
    assert cfg.pipeline.universe_stale_days == 14
    legacy = load_config(overrides={"pipeline": {"jpx400_stale_days": 3}})
    assert legacy.pipeline.universe_stale_days == 14         # 新キーが YAML にあるので旧キーは無視される
    assert date(2026, 10, 30).isoformat() == "2026-10-30"
