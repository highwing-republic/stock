import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from synth import business_days, insert_filing, insert_prices, make_path, membership, price_rows  # noqa: E402

from src.radar import connect  # noqa: E402
from src.watch.config import load_config  # noqa: E402
from src.watch.store import write_json  # noqa: E402

JST = timezone(timedelta(hours=9))


@pytest.fixture(scope="session")
def cfg():
    return load_config()


class Env:
    pass


@pytest.fixture()
def env(tmp_path, cfg):
    """~300 営業日の合成 DB（6 銘柄＋1306.T）と membership を用意する。"""
    e = Env()
    e.cfg = cfg
    e.days = business_days(date(2025, 8, 1), 300)
    n = len(e.days)
    e.as_of = e.days[-1]
    e.now = datetime(e.as_of.year, e.as_of.month, e.as_of.day, 21, 5, tzinfo=JST)
    e.db_path = tmp_path / "radar.db"
    e.state = tmp_path / "state"
    e.out = tmp_path / "public" / "watch"
    conn = connect(e.db_path)
    e.conn = conn
    up = dict(drift=0.0012, vol=0.004)
    insert_prices(conn, price_rows("1306.T", e.days, make_path(n, 1, 2000, 0.0002, 0.006), seed=1))
    insert_prices(conn, price_rows("1001.T", e.days, make_path(n, 2, 1000, **up), seed=2,
                                   vol_spikes={n - 3: 2.0}))
    insert_prices(conn, price_rows("1002.T", e.days, make_path(n, 3, 1500, 0.0007, 0.004), seed=3))
    insert_prices(conn, price_rows("1003.T", e.days, make_path(n, 4, 800, **up), seed=4, split_at=260,
                                   skip={150, 151, 270}))
    insert_prices(conn, price_rows("1004.T", e.days, make_path(n, 5, 900, **up), seed=5))
    insert_prices(conn, price_rows("1005.T", e.days, make_path(n, 6, 700, 0.0008, 0.004), seed=6))
    insert_prices(conn, price_rows("1006.T", e.days, make_path(n, 7, 600, 0.0, 0.02), seed=7))
    d = lambda i: e.days[i].isoformat()  # noqa: E731
    # 1001: 新規大量保有
    insert_filing(conn, "S1001A", f"{d(292)} 10:00", "1001", report_type="initial", ratio=6.4, name="ワン工業")
    # 1002: 大幅買い増し → 30 日以内の連続買い増し（同一 Episode に統合）＋訂正
    insert_filing(conn, "S1002A", f"{d(240)} 09:10", "1002", ratio=6.5, prev=5.0, filer="大和証券", name="ツー化学")
    insert_filing(conn, "S1002B", f"{d(255)} 14:00", "1002", ratio=7.6, prev=6.5, filer="大和証券", name="ツー化学")
    insert_filing(conn, "S1002C", f"{d(260)} 10:00", "1002", report_type="correction", ratio=7.6, prev=6.5,
                  filer="大和証券", name="ツー化学", desc="訂正変更報告書")
    # 1003: 新規大量保有の後に 2:1 分割（分割が誤って BROKEN 扱いにならないこと）
    insert_filing(conn, "S1003A", f"{d(240)} 11:00", "1003", report_type="initial", ratio=5.2, name="スリー電機")
    # 1004: 非メンバー（強い材料でもランキングに出ない）
    insert_filing(conn, "S1004A", f"{d(250)} 10:00", "1004", report_type="initial", ratio=9.9, name="フォー商事")
    # 1005: 小さな買い増し（トリガーにならない）
    insert_filing(conn, "S1005A", f"{d(250)} 10:00", "1005", ratio=5.4, prev=5.0, name="ファイブ建設")
    # 1006: 新規大量保有だが Setup が成立しない
    insert_filing(conn, "S1006A", f"{d(200)} 10:00", "1006", report_type="initial", ratio=5.5, name="シックス食品")
    conn.execute("INSERT INTO sync_history(target_date,status,documents_count,filings_count,synced_at) "
                 "VALUES(?,?,?,?,?)", (d(299), "ok", 10, 8, f"{d(299)} 08:00:00"))
    conn.commit()
    e.membership = membership({"1001": "ワン工業", "1002": "ツー化学", "1003": "スリー電機", "1005": "ファイブ建設",
                               "1006": "シックス食品"}, checked=e.now.isoformat(timespec="seconds"))
    write_json(e.state / "jpx400_membership.json", e.membership)
    return e


def run_pipeline(e, **kw):
    from src.watch import pipeline
    args = dict(db_path=e.db_path, state_dir=e.state, out_dir=e.out, cfg=e.cfg, as_of=e.as_of, offline=True,
                now=e.now)
    args.update(kw)
    return pipeline.run(**args)
