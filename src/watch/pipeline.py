"""全体オーケストレーション・品質ゲート・job_runs。"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import events as events_mod
from . import export, prices, ranking
from .calendar import MarketCalendar
from .config import Cfg, load_config
from .episodes import replay_code
from .indicators import compute_indicators
from .setups import evaluate_setups, setup_changes
from .store import Store

LOG = logging.getLogger("watch.pipeline")
JST = timezone(timedelta(hours=9))
ROW_COLUMNS = ["close", "prev_close", "ma20", "ma60", "ma200", "ma20_slope5", "atr_pct_rank252", "range_pos60",
               "dist_52w_high", "vol_ratio20", "rel20", "rel60", "ret5", "primary", "breakout", "recovery",
               "trend", "improved", "milestone_any", "improve_recent", "new_high60", "ma20_reclaimed",
               "ma60_reclaimed", "rel20_pos"]


class QualityGateError(Exception):
    pass


@dataclass
class RunResult:
    status: str                       # ok | failed
    published: bool = False
    message: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class Computed:
    days: list[str]
    day_index: dict[str, int]
    coverage: float
    universe_count: int
    names: dict[str, str]
    events: list[dict]
    episodes: list[dict]
    setup_changes: list[dict]
    rows: dict[str, dict[str, dict]]
    series: Callable[[str], tuple[list[str], list[float]]]
    bench_close: dict[str, float]
    rankings: dict[str, dict]         # kind → {"key","entries","previous"}
    ranking_writes: list[tuple[str, str, dict]]
    margin_views: dict[str, dict]


def _iso(dt: datetime) -> str:
    return dt.astimezone(JST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- 計算本体（純粋。書き込みなし）
def compute(conn: sqlite3.Connection, universe, store: Store, cfg: Cfg, as_of: date,
            margin_history: dict | None = None) -> Computed:
    bench_ticker = cfg.pipeline.benchmark_ticker
    codes = sorted(universe.all_codes())
    tickers = {c: f"{c}.T" for c in codes}
    frames = prices.load_adjusted(conn, list(tickers.values()) + [bench_ticker])
    bench = frames.get(bench_ticker)
    if bench is None or len(bench) == 0:
        raise QualityGateError(f"benchmark {bench_ticker} prices missing")
    cal = MarketCalendar(bench.index.date, cfg.calendar.extra_holidays, cfg.calendar.market_close)
    if (as_of - cal.last).days > cfg.prices.max_stale_days:
        raise QualityGateError(f"prices are stale: latest benchmark date {cal.last}")
    end = min(as_of, cal.last)
    window_start = as_of - timedelta(days=cfg.pipeline.replay_days)
    days = [d.isoformat() for d in cal.business_days(window_start + timedelta(days=1), end)]
    if len(days) < 20:
        raise QualityGateError(f"too few business days in replay window: {len(days)}")
    day_index = {d: i for i, d in enumerate(days)}
    last = days[-1]
    last_ts = pd.Timestamp(last)

    members_last = universe.members_on(date.fromisoformat(last))
    if not members_last:
        raise QualityGateError("no JPX400 members on the latest business day")
    have = sum(1 for c in members_last if tickers[c] in frames and last_ts in frames[tickers[c]].index)
    coverage = have / len(members_last)
    if coverage < cfg.prices.min_coverage:
        raise QualityGateError(f"price coverage {coverage:.3f} < {cfg.prices.min_coverage}")

    @lru_cache(maxsize=None)
    def is_member(code: str, on: date) -> bool:
        return bool(universe.is_member(code, on))

    # ---- イベント
    filings = events_mod.load_filings(conn)
    events_all = events_mod.normalize_events(filings, cal, cfg, universe, codes=set(codes))
    events = events_mod.in_window(events_all, window_start.isoformat())
    events_by_id = {e["event_id"]: e for e in events_all}

    # ---- 指標・Setup
    bench_close = bench["close"]
    sfs: dict[str, pd.DataFrame] = {}
    for code in codes:
        f = frames.get(tickers[code])
        if f is not None and len(f) >= 2:
            sfs[code] = evaluate_setups(compute_indicators(f, bench_close, cfg), cfg)
    day_set = set(days)
    changes = []
    for code, sf in sfs.items():
        for ch in setup_changes(code, sf, day_set):
            if is_member(code, date.fromisoformat(ch["date"])):
                changes.append(ch)
    changes.sort(key=lambda c: (c["date"], c["security_code"], c["kind"], c.get("milestone") or ""))

    bench_map = {ts.strftime("%Y-%m-%d"): float(v) for ts, v in bench_close.items()}
    day_ts = pd.DatetimeIndex(days)
    triggers = set(cfg.events.trigger_types)
    trigger_codes = sorted({e["security_code"] for e in events
                            if e["event_type"] in triggers and e["event_market_date"] >= days[0]})
    names: dict[str, str] = {}
    for code in codes:
        names[code] = universe.company_name(code) or ""
    for e in events_all:
        if not names.get(e["security_code"]):
            names[e["security_code"]] = e["company_name"] or e["security_code"]
    rows: dict[str, dict[str, dict]] = {}
    episodes: list[dict] = []
    traces_by_code: dict[str, dict] = {}
    for code in trigger_codes:
        sf = sfs.get(code)
        if sf is None:
            continue
        sub = sf.loc[sf.index.isin(day_ts), ROW_COLUMNS].copy()
        sub.index = sub.index.strftime("%Y-%m-%d")
        rows[code] = sub.to_dict("index")
        eps, traces = replay_code(code, names.get(code) or code, events, days, day_index, is_member, rows[code],
                                  bench_map, cfg)
        episodes.extend(eps)
        traces_by_code[code] = traces
    episodes.sort(key=lambda e: (e["started_at"], e["episode_id"]))
    episodes_by_id = {e["episode_id"]: e for e in episodes}

    # ---- 信用残ビュー（最新日のみ使用）
    views: dict[str, dict] = {}
    if margin_history:
        from .sources.margin import margin_view
        for code in trigger_codes:
            v = margin_view(margin_history.get(code, []), surge_pct=cfg.margin.surge_pct, drop_pct=cfg.margin.drop_pct)
            if v:
                views[code] = v

    def margin_bonus(code: str, day: str) -> bool:
        v = views.get(code)
        return bool(day == last and v and v.get("buyChangePct") is not None and v["buyChangePct"] < 0)

    daily = ranking.build_daily_scores(traces_by_code, episodes_by_id, events_by_id, days, day_index, is_member,
                                       cfg, margin_bonus)

    # ---- ランキング（現在期間＋未凍結の直近期間）
    rankings: dict[str, dict] = {}
    writes: list[tuple[str, str, dict]] = []
    first_prev = cal.prev_business_day(date.fromisoformat(days[0])).isoformat()
    for kind in ("weekly", "monthly"):
        keys = sorted({ranking.period_key(kind, d) for d in days})
        cur_key = ranking.period_key(kind, last)
        back = cfg.ranking.backfill_weekly if kind == "weekly" else cfg.ranking.backfill_monthly
        wanted = [k for k in keys if k < cur_key][-back:] + [cur_key]
        snaps: dict[str, dict] = {}
        for key in wanted:
            existing = store.read_ranking(kind, key)
            if existing and existing.get("final"):
                snaps[key] = existing
                continue
            if ranking.period_key(kind, first_prev) == key and key != cur_key:
                continue  # 窓の先頭で欠けた期間は確定させない
            pdays = [d for d in days if ranking.period_key(kind, d) == key]
            entries = ranking.run_period(kind, pdays, daily, episodes_by_id, events_by_id, cfg)
            snap = ranking.snapshot(kind, key, pdays[-1], entries, final=(key != cur_key))
            snaps[key] = snap
            writes.append((kind, key, snap))
        prev_keys = [k for k in keys if k < cur_key]
        prev_snap = snaps.get(prev_keys[-1]) if prev_keys else None
        if prev_snap is None and prev_keys:
            prev_snap = store.read_ranking(kind, prev_keys[-1])
        rankings[kind] = {"key": cur_key, "entries": snaps[cur_key]["entries"],
                          "previous": prev_snap["entries"] if prev_snap else None}

    series_cache: dict[str, tuple[list[str], list[float]]] = {}

    def series(code: str) -> tuple[list[str], list[float]]:
        if code not in series_cache:
            sf = sfs.get(code)
            if sf is None:
                series_cache[code] = ([], [])
            else:
                series_cache[code] = (list(sf.index.strftime("%Y-%m-%d")), [float(x) for x in sf["close"]])
        return series_cache[code]

    return Computed(days=days, day_index=day_index, coverage=coverage,
                    universe_count=len(members_last), names=names, events=events, episodes=episodes,
                    setup_changes=changes, rows=rows, series=series, bench_close=bench_map, rankings=rankings,
                    ranking_writes=writes, margin_views=views)


# --------------------------------------------------------------------------- 実行
def _edinet_source(conn: sqlite3.Connection, now: datetime, cfg: Cfg) -> dict:
    row = conn.execute("SELECT MAX(synced_at) FROM sync_history WHERE status IN ('ok','partial')").fetchone()
    if not row or not row[0]:
        return {"status": "failed", "lastSuccessAt": None}
    synced = datetime.fromisoformat(row[0].replace(" ", "T")).replace(tzinfo=timezone.utc)
    stale = (now - synced) > timedelta(days=cfg.pipeline.edinet_stale_days)
    return {"status": "stale" if stale else "ok", "lastSuccessAt": _iso(synced)}


def _job(source: str, started: datetime, finished: datetime, status: str, records: int | None = None,
         error: str | None = None, detail: Any = None) -> dict:
    job = {"source": source, "started_at": _iso(started), "finished_at": _iso(finished), "status": status,
           "records": records, "error": (error or None) and str(error)[:500]}
    if detail:
        job["detail"] = detail
    return job


def run(*, db_path: Path | None = None, state_dir: Path, out_dir: Path, cfg: Cfg | None = None,
        as_of: date | None = None, offline: bool = False, now: datetime | None = None,
        downloader=None, jpx_fetcher=None, margin_fetcher=None) -> RunResult:
    from .universe import (Universe, apply_adjustments, apply_snapshot, dumps_membership, load_adjustments,
                           load_membership, save_membership)
    from ..radar import connect

    cfg = cfg or load_config()
    fixed_now = now is not None
    now = now or datetime.now(JST)
    as_of = as_of or now.astimezone(JST).date()
    store = Store(state_dir)
    conn = connect(db_path)
    runs: list[dict] = []
    finish = lambda: now if fixed_now else datetime.now(JST)  # noqa: E731

    def done(status: str, message: str, published: bool = False, **details) -> RunResult:
        runs.append(_job("watch", now, finish(), "ok" if status == "ok" else "failed", details.get("records"),
                         None if status == "ok" else message))
        store.append_job_runs(as_of.isoformat()[:7], runs)
        return RunResult(status, published, message, details)

    loaded = load_membership(store.membership_path)
    membership = loaded
    jpx = {"status": None}
    try:
        # ---- JPX400 更新確認（取得→ apply_snapshot、その上に公表済みの随時変更 apply_adjustments）
        cal_db = MarketCalendar(pd.read_sql_query("SELECT trade_date FROM prices WHERE ticker=?", conn,
                                                  params=[cfg.pipeline.benchmark_ticker])["trade_date"],
                                cfg.calendar.extra_holidays, cfg.calendar.market_close)
        pbd = cal_db.prev_business_day
        reports: dict[str, Any] = {}
        started, fetch_ok, fetch_error = now, False, None
        if not offline:
            try:
                if jpx_fetcher is None:
                    from .sources.jpx400 import fetch_constituents as jpx_fetcher
                snap = jpx_fetcher()
                membership, rep = apply_snapshot(membership, snap, min_members=cfg.universe.min_members,
                                                 previous_business_day=pbd)
                reports["snapshot"] = rep
                if rep["status"] == "rejected":
                    raise ValueError(rep.get("error") or "membership update rejected")
                fetch_ok = True
            except Exception as exc:
                LOG.warning("JPX400 update failed; keeping stored membership: %s", exc)
                fetch_error = exc
        adj_error = None
        try:
            membership, rep2 = apply_adjustments(membership, load_adjustments(store.adjustments_path),
                                                 previous_business_day=pbd)
            reports["adjustments"] = rep2
        except Exception as exc:  # 調整ファイル不正でも本体は続行
            LOG.warning("JPX400 adjustments skipped: %s", exc)
            adj_error = exc
        if fetch_ok:
            membership = dict(membership)
            membership["last_checked_at"] = _iso(now)
        new_membership = membership if dumps_membership(membership) != dumps_membership(loaded) else None
        if not offline:
            jpx = {"status": "ok" if fetch_ok else "stale"}
            err = fetch_error or adj_error
            runs.append(_job("jpx400", started, finish(), "ok" if fetch_ok and not adj_error else
                             ("partial" if fetch_ok else "failed"),
                             (reports.get("snapshot") or reports.get("adjustments") or {}).get("member_count"),
                             (str(err) if err else None), reports))
            if reports:
                LOG.info("jpx400 reports: %s", reports)
        universe = Universe(membership)
        if not universe.all_codes():
            return done("failed", "JPX400 membership is empty; public JSON left untouched")

        # ---- 価格
        price_run: dict[str, Any] = {"ok": None}
        if not offline:
            started = now
            try:
                tickers = [f"{c}.T" for c in universe.all_codes()] + [cfg.pipeline.benchmark_ticker]
                rep = prices.update_prices(conn, tickers, as_of, cfg, downloader=downloader)
                status = "partial" if rep["failed"] else "ok"
                runs.append(_job("prices", started, finish(), status, rep["rows"],
                                 "; ".join(rep["failed"][:5]) if rep["failed"] else None))
                price_run["ok"] = True
            except Exception as exc:
                LOG.exception("price update failed")
                runs.append(_job("prices", started, finish(), "failed", error=exc))

        # ---- 信用残（任意）
        history = store.read_margin_history()
        latest = store.read_margin_latest()
        margin_update = None
        margin_status = "disabled"
        if cfg.margin.enabled:
            margin_status = "ok" if latest else "disabled"
            if latest and (as_of - date.fromisoformat(latest["as_of"])).days > cfg.margin.stale_days:
                margin_status = "stale"
            if not offline:
                started = now
                try:
                    if margin_fetcher is None:
                        from .sources.margin import fetch_latest as margin_fetcher
                    from .sources import margin as margin_mod
                    codes_now = universe.members_on(as_of)
                    snap = margin_fetcher(universe_codes=codes_now, min_rows=cfg.margin.min_rows,
                                          min_universe_coverage=cfg.margin.min_universe_coverage)
                    snap = margin_mod.filter_to(snap, universe.all_codes())
                    history = margin_mod.merge_history(history, snap, keep=cfg.margin.keep)
                    margin_update = ({"schema_version": 1, "as_of": snap.as_of.isoformat(), "kind": snap.kind,
                                      "source_url": snap.source_url, "count": len(snap.rows)}, history)
                    latest = margin_update[0]
                    margin_status = "ok"
                    runs.append(_job("margin", started, finish(), "ok", len(snap.rows)))
                except Exception as exc:
                    LOG.warning("margin update failed; keeping previous data: %s", exc)
                    margin_status = "stale" if latest else "failed"
                    runs.append(_job("margin", started, finish(), "failed", error=exc))

        # ---- 計算
        c = compute(conn, universe, store, cfg, as_of, history if cfg.margin.enabled else None)

        # ---- 公開 JSON の組み立て・検証
        prices_ok = store.last_success("prices")
        if price_run["ok"]:
            prices_ok = _iso(now)
        if offline:
            checked = membership.get("last_checked_at")
            jpx_status = "stale"
            if checked:
                stamp = datetime.fromisoformat(checked)
                stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=JST)
                if now - stamp <= timedelta(days=cfg.pipeline.jpx400_stale_days):
                    jpx_status = "ok"
        else:
            jpx_status = jpx["status"]
        sources = {"edinet": _edinet_source(conn, now, cfg),
                   "prices": {"status": "ok", "lastSuccessAt": prices_ok, "coverage": round(c.coverage, 4)},
                   "jpx400": {"status": jpx_status, "lastSuccessAt": membership.get("last_checked_at"),
                              "memberCount": c.universe_count},
                   "margin": {"status": margin_status, "asOf": latest["as_of"] if latest and margin_status != "disabled"
                              else None}}
        ctx = export.PublicContext(
            cfg=cfg, now=_iso(now), as_of=c.days[-1], days=c.days, day_index=c.day_index, sources=sources,
            universe_count=c.universe_count, names=c.names, weekly=c.rankings["weekly"],
            monthly=c.rankings["monthly"], episodes=c.episodes, events=c.events, setup_changes=c.setup_changes,
            rows=c.rows, series=c.series, bench_close=c.bench_close, margin_views=c.margin_views)
        files = export.build_public(ctx)
        export.validate_public(files)
    except (QualityGateError, export.ExportValidationError) as exc:
        LOG.error("aborted before publishing: %s", exc)
        return done("failed", str(exc))
    except Exception as exc:
        LOG.exception("watch pipeline crashed")
        return done("failed", f"{type(exc).__name__}: {exc}")

    # ---- 公開 → state 保存（ここまで来たら失敗させない）
    export.publish(files, out_dir)
    if new_membership is not None:
        save_membership(store.membership_path, new_membership)
    store.write_events(events_mod.events_by_month(c.events))
    store.write_episodes(c.episodes)
    store.write_setup_history(c.setup_changes)
    for kind, key, snap in c.ranking_writes:
        store.write_ranking(kind, key, snap)
    if margin_update:
        store.write_margin(*margin_update)
    return done("ok", "published", True, records=len(files), asOf=c.days[-1], coverage=round(c.coverage, 4))
