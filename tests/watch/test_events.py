from datetime import date

from synth import insert_filing

from src.radar import connect
from src.watch.calendar import MarketCalendar
from src.watch.events import events_by_month, load_filings, normalize_events


class U:
    def is_member(self, code, on):
        return code == "1001"


def norm(cfg, filings, **kw):
    return normalize_events(filings, MarketCalendar([], [], "15:30"), cfg, **kw)


def f(doc, dt, code="1001", **kw):
    base = {"doc_id": doc, "submit_datetime": dt, "security_code": code, "filer_name": "A証券",
            "issuer_name": "テスト", "report_type": "change", "holding_ratio": None,
            "previous_holding_ratio": None, "holding_change": None, "important_proposal": 0,
            "doc_description": "変更報告書"}
    base.update(kw)
    return base


def test_types_and_priority(cfg):
    ev = norm(cfg, [
        f("N", "2026-09-01 10:00", report_type="initial", holding_ratio=6.4),
        f("C", "2026-09-01 11:00", report_type="correction", holding_change=2.0),
        f("U", "2026-09-01 12:00"),
        f("D", "2026-09-01 13:00", holding_change=-1.2),
        f("I", "2026-09-02 10:00", code="1002", holding_change=0.4),
        f("L", "2026-09-02 10:00", code="1003", holding_change=1.5, important_proposal=1),
    ], universe=U())
    by = {e["source_document_id"]: e for e in ev}
    assert by["N"]["event_type"] == "NEW_HOLDER" and by["N"]["event_id"] == "edinet:N"
    assert by["C"]["event_type"] == "CORRECTION"
    assert by["U"]["event_type"] == "CHANGE_UNKNOWN"
    assert by["D"]["event_type"] == "DECREASE"
    assert by["I"]["event_type"] == "INCREASE"
    assert by["L"]["event_type"] == "LARGE_INCREASE" and "PURPOSE_CHANGE" in by["L"]["tags"]
    assert by["N"]["is_jpx400_at_event"] is True and by["I"]["is_jpx400_at_event"] is False
    assert by["N"]["source_url"] == "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?N"
    assert [e["event_id"] for e in ev] == sorted(e["event_id"] for e in ev)


def test_consecutive_and_dedupe(cfg):
    rows = [f("A", "2026-09-01 10:00", holding_change=1.5), f("A", "2026-09-01 10:00", holding_change=1.5),
            f("B", "2026-09-20 10:00", holding_change=0.5),
            f("C", "2026-11-25 10:00", holding_change=0.5),          # 30 日超 → 連続ではない
            f("D", "2026-09-25 10:00", holding_change=0.3, filer_name="B証券")]  # 別提出者
    ev = {e["source_document_id"]: e for e in norm(cfg, rows)}
    assert len(ev) == 4
    assert ev["A"]["event_type"] == "LARGE_INCREASE"
    assert ev["B"]["event_type"] == "CONSECUTIVE_INCREASE" and "CONSECUTIVE_INCREASE" in ev["B"]["tags"]
    assert ev["C"]["event_type"] == "INCREASE"
    assert ev["D"]["event_type"] == "INCREASE"


def test_correction_does_not_count_as_prior_increase(cfg):
    rows = [f("A", "2026-09-01 10:00", report_type="correction", holding_change=2.0),
            f("B", "2026-09-10 10:00", holding_change=0.5)]
    ev = {e["source_document_id"]: e for e in norm(cfg, rows)}
    assert ev["B"]["event_type"] == "INCREASE"


def test_market_date_and_months(cfg):
    ev = norm(cfg, [f("A", "2026-09-04 16:00", holding_change=1.5), f("B", "2026-10-01 09:00", holding_change=1.5)])
    assert ev[0]["event_market_date"] == "2026-09-07"
    assert list(events_by_month(ev)) == ["2026-09", "2026-10"]


def test_load_filings_from_db(tmp_path, cfg):
    conn = connect(tmp_path / "t.db")
    insert_filing(conn, "X1", "2026-09-01 10:00", "1001", ratio=6.0, prev=5.0)
    conn.execute("INSERT INTO filings(doc_id,submit_datetime,report_type) VALUES('NOCODE','2026-09-01 10:00','change')")
    conn.commit()
    rows = load_filings(conn)
    assert [r["doc_id"] for r in rows] == ["X1"]
    ev = norm(cfg, rows, codes={"1001"})
    assert ev[0]["event_type"] == "LARGE_INCREASE" and ev[0]["holding_change"] == 1.0
