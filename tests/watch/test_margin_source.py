from datetime import date
from pathlib import Path

import pytest

from src.watch.sources import margin
from src.watch.sources.margin import (MarginRow, MarginSnapshot, MarginValidationError,
                                      fetch_latest, filter_to, margin_view, merge_history,
                                      parse_daily_text, parse_weekly_text, validate)

FIX = Path(__file__).parent / "fixtures"


def daily_text():
    return (FIX / "margin_daily_20260928.txt").read_text(encoding="utf-8")


def weekly_text():
    return (FIX / "margin_weekly_20260918.txt").read_text(encoding="utf-8")


def test_parse_daily_fixture():
    snap = parse_daily_text(daily_text(), source_url="u")
    assert snap.kind == "daily" and snap.as_of == date(2026, 9, 28)
    kyokuyo = snap.rows["1301"]
    assert kyokuyo == MarginRow("1301", buy_balance=154400, sell_balance=9500,
                                buy_value=704076000, sell_value=45060500,
                                general_buy=35400, general_sell=100,
                                institutional_buy=119000, institutional_sell=9400)
    assert "9508" in snap.rows and len(snap.rows) == 12


def test_daily_stray_spaces_inside_numbers_are_repaired():
    # 1802: "81,7 00", "113,3 46,131" style breaks; totals must equal general + institutional
    row = parse_daily_text(daily_text(), source_url="u").rows["1802"]
    assert row.sell_balance == 148600 and row.buy_balance == 740500
    assert row.general_sell + row.institutional_sell == row.sell_balance
    assert row.general_buy + row.institutional_buy == row.buy_balance
    assert row.buy_value == 2450928700  # value line has "Va l." glitch


def test_daily_alphanumeric_code_and_page_break_split():
    snap = parse_daily_text(daily_text(), source_url="u")
    assert snap.rows["137A"].buy_balance == 104600  # code "137A0" -> "137A"
    # 1376: shares row at the end of a page, value row after the next page's header
    kaneko = snap.rows["1376"]
    assert kaneko.buy_balance == 34000 and kaneko.buy_value == 52636800


def test_daily_etf_star_ratio_rows_are_parsed():
    snap = parse_daily_text(daily_text(), source_url="u")
    if "1326" in snap.rows:
        assert snap.rows["1326"].buy_balance is not None


def test_daily_requires_as_of():
    with pytest.raises(MarginValidationError):
        parse_daily_text("no header\nB x 13010 JP3257200000 株数 Shs. 1 2 3", source_url="u")


def test_daily_no_rows_is_error():
    with pytest.raises(MarginValidationError):
        parse_daily_text("2026/9/28 申込み現在\nnothing", source_url="u")


def test_parse_weekly_fixture():
    snap = parse_weekly_text(weekly_text(), source_url="u")
    assert snap.kind == "weekly" and snap.as_of == date(2026, 9, 18)
    row = snap.rows["1301"]
    assert (row.sell_balance, row.buy_balance) == (9300, 156100)
    assert (row.general_sell, row.institutional_sell) == (100, 9200)
    assert (row.general_buy, row.institutional_buy) == (35900, 120200)
    assert row.buy_value is None and row.sell_value is None
    # "2 53,500" repaired
    assert snap.rows["1802"].general_buy == 253500
    assert snap.rows["137A"].sell_balance == 0
    assert len(snap.rows) == 9


def test_validate_failures():
    snap = parse_daily_text(daily_text(), source_url="u")
    validate(snap, {"1301", "1802"}, min_rows=5, min_universe_coverage=0.9)
    with pytest.raises(MarginValidationError, match="行数"):
        validate(snap, {"1301"}, min_rows=2000, min_universe_coverage=0.9)
    with pytest.raises(MarginValidationError, match="カバー率"):
        validate(snap, {"1301", "1802", "7203", "9984"}, min_rows=5, min_universe_coverage=0.9)
    zeros = MarginSnapshot(snap.as_of, "daily", "u",
                           {c: MarginRow(c, 0, 0, 0, 0, 0, 0, 0, 0) for c in snap.rows})
    with pytest.raises(MarginValidationError, match="すべて 0"):
        validate(zeros, {"1301"}, min_rows=5, min_universe_coverage=0.9)


def test_filter_to_keeps_only_requested_codes():
    snap = parse_daily_text(daily_text(), source_url="u")
    small = filter_to(snap, {"1301", "9999"})
    assert set(small.rows) == {"1301"} and small.as_of == snap.as_of and small.kind == "daily"
    assert len(snap.rows) == 12  # original untouched


# ---------------------------------------------------------------- fetch with fake session

INDEX_HTML = """
<a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/20260928_mtall.pdf">2026年9月28日申込分</a>
<a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/20260925_mtall.pdf">2026年9月25日申込分</a>
<a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026091800.pdf">9/18</a>
<a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026091100.pdf">9/11</a>
"""
BASE = "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/"


class Resp:
    def __init__(self, content, status=200):
        self.content, self.status = content, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if url not in self.routes:
            return Resp(b"", 404)
        return self.routes[url]


@pytest.fixture(autouse=True)
def text_as_pdf(monkeypatch):
    monkeypatch.setattr(margin, "extract_pdf_text", lambda content: content.decode("utf-8"))


def routes(daily=True, daily_prev=False, weekly=True):
    r = {margin.INDEX_URL: Resp(INDEX_HTML.encode("utf-8"))}
    if daily:
        r[BASE + "20260928_mtall.pdf"] = Resp(daily_text().encode("utf-8"))
    if daily_prev:
        r[BASE + "20260925_mtall.pdf"] = Resp(daily_text().replace("2026/9/28", "2026/9/25").encode("utf-8"))
    if weekly:
        r[BASE + "syumatsu2026091800.pdf"] = Resp(weekly_text().encode("utf-8"))
    return r


KW = dict(universe_codes={"1301", "1802", "1803"}, min_rows=5, min_universe_coverage=0.9)


def test_discover_links_newest_first():
    links = margin.discover_links(INDEX_HTML)
    assert links["daily"][0].endswith("20260928_mtall.pdf") and links["daily"][0].startswith("https://")
    assert links["weekly"][0].endswith("syumatsu2026091800.pdf")


def test_fetch_prefers_daily():
    session = Session(routes())
    snap = fetch_latest(session, **KW)
    assert snap.kind == "daily" and snap.as_of == date(2026, 9, 28)
    assert snap.source_url.endswith("20260928_mtall.pdf")
    assert not any("syumatsu" in c for c in session.calls)


def test_fetch_falls_back_to_weekly_when_daily_missing():
    snap = fetch_latest(Session(routes(daily=False)), **KW)
    assert snap.kind == "weekly" and snap.as_of == date(2026, 9, 18)


def test_fetch_falls_back_when_daily_fails_validation():
    r = routes()
    r[BASE + "20260928_mtall.pdf"] = Resp(b"2026/9/28 \xe7\x94\xb3\xe8\xbe\xbc\xe3\x81\xbf\xe7\x8f\xbe\xe5\x9c\xa8\ngarbage")
    snap = fetch_latest(Session(r), **KW)
    assert snap.kind == "weekly"


def test_fetch_uses_previous_daily_if_latest_is_bad():
    r = routes(daily_prev=True)
    del r[BASE + "20260928_mtall.pdf"]
    snap = fetch_latest(Session(r), **KW)
    assert snap.kind == "daily" and snap.as_of == date(2026, 9, 25)


def test_fetch_prefer_weekly_option():
    snap = fetch_latest(Session(routes()), prefer_daily=False, **KW)
    assert snap.kind == "weekly"


def test_fetch_raises_when_both_fail():
    with pytest.raises(MarginValidationError) as exc:
        fetch_latest(Session(routes(daily=False, weekly=False)), **KW)
    assert "daily" in str(exc.value) and "weekly" in str(exc.value)


def test_fetch_raises_when_coverage_too_low_everywhere():
    kw = dict(KW, universe_codes={"1301", "7203", "9984", "6758x"})
    with pytest.raises(MarginValidationError):
        fetch_latest(Session(routes()), **kw)


def test_fetch_raises_when_index_page_unavailable():
    with pytest.raises(MarginValidationError, match="一覧ページ"):
        fetch_latest(Session({}), **KW)


# ---------------------------------------------------------------- history / view

def snapshot(as_of, kind, buy, sell, code="1301"):
    row = MarginRow(code, buy, sell, buy * 10 if buy is not None else None, None, None, None, None, None)
    return MarginSnapshot(as_of, kind, "u", {code: row})


def test_merge_history_dedupes_by_as_of_and_keeps_order():
    h = merge_history({}, snapshot(date(2026, 9, 28), "daily", 100, 10))
    h = merge_history(h, snapshot(date(2026, 9, 25), "daily", 90, 10))
    h = merge_history(h, snapshot(date(2026, 9, 28), "daily", 105, 10))  # same day replaces
    assert [e["as_of"] for e in h["1301"]] == ["2026-09-25", "2026-09-28"]
    assert h["1301"][-1]["buy_balance"] == 105
    # weekly must not overwrite a daily entry for the same date; daily overrides weekly
    h = merge_history(h, snapshot(date(2026, 9, 28), "weekly", 1, 1))
    assert h["1301"][-1]["kind"] == "daily" and h["1301"][-1]["buy_balance"] == 105
    h2 = merge_history({}, snapshot(date(2026, 9, 18), "weekly", 50, 5))
    h2 = merge_history(h2, snapshot(date(2026, 9, 18), "daily", 51, 5))
    assert len(h2["1301"]) == 1 and h2["1301"][0]["kind"] == "daily"


def test_merge_history_keep_limit_and_no_mutation():
    base = {}
    for i in range(1, 8):
        base = merge_history(base, snapshot(date(2026, 9, i), "daily", 100 + i, 10), keep=5)
    assert len(base["1301"]) == 5 and base["1301"][0]["as_of"] == "2026-09-03"
    before = {k: [dict(e) for e in v] for k, v in base.items()}
    merge_history(base, snapshot(date(2026, 9, 20), "daily", 1, 1))
    assert base == before


def test_merge_history_keeps_codes_absent_from_snapshot():
    h = merge_history({}, snapshot(date(2026, 9, 25), "daily", 1, 1, code="1802"))
    h = merge_history(h, snapshot(date(2026, 9, 28), "daily", 1, 1, code="1301"))
    assert set(h) == {"1802", "1301"}


def hist(pairs, kind="daily"):
    return [{"as_of": d, "kind": kind, "buy_balance": b, "sell_balance": s} for d, b, s in pairs]


def test_margin_view_none_and_single_entry():
    assert margin_view([]) is None
    v = margin_view(hist([("2026-09-28", 100, 10)]))
    assert v["buyBalance"] == 100 and v["ratio"] == 10.0 and v["labels"] == []
    assert v["buyChangePct"] is None and v["asOf"] == "2026-09-28"


def test_margin_view_buy_decrease_and_ratio_improvement():
    # 2026-09-28 (Mon) vs 5 weekdays earlier = 2026-09-21
    v = margin_view(hist([("2026-09-21", 100, 10), ("2026-09-25", 95, 10), ("2026-09-28", 80, 10)]))
    assert v["buyChangePct"] == -20.0 and v["sellChangePct"] == 0.0
    assert v["ratio"] == 8.0
    assert v["labels"] == ["信用買残減少", "信用倍率改善"]


def test_margin_view_surge_and_deterioration_and_sell_increase():
    v = margin_view(hist([("2026-09-21", 100, 10), ("2026-09-28", 130, 10)]))
    assert v["labels"] == ["信用買残急増", "信用倍率悪化"]
    v = margin_view(hist([("2026-09-21", 100, 10), ("2026-09-28", 100, 13)]))
    assert "売残増加" in v["labels"] and "信用倍率改善" in v["labels"]


def test_margin_view_no_label_within_thresholds():
    v = margin_view(hist([("2026-09-21", 100, 10), ("2026-09-28", 105, 10)]))
    assert v["labels"] == [] and v["buyChangePct"] == 5.0


def test_margin_view_custom_thresholds():
    v = margin_view(hist([("2026-09-21", 100, 10), ("2026-09-28", 95, 10)]), drop_pct=0.05)
    assert "信用買残減少" in v["labels"]


def test_margin_view_weekly_compares_previous_week():
    v = margin_view(hist([("2026-09-11", 100, 10), ("2026-09-18", 70, 10)], kind="weekly"))
    assert v["buyChangePct"] == -30.0 and "信用買残減少" in v["labels"]


def test_margin_view_no_reference_old_enough():
    v = margin_view(hist([("2026-09-25", 100, 10), ("2026-09-28", 50, 10)]))
    assert v["buyChangePct"] is None and v["labels"] == []


def test_margin_view_handles_zero_sell_and_none_values():
    v = margin_view(hist([("2026-09-21", 100, 0), ("2026-09-28", 80, 0)]))
    assert v["ratio"] is None and v["labels"] == ["信用買残減少"]
    v = margin_view(hist([("2026-09-21", None, None), ("2026-09-28", 80, 5)]))
    assert v["buyChangePct"] is None
