"""EDINET filings → 正規化イベント（書類単位・決定的）。"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Iterable

from .calendar import MarketCalendar, to_date

EDINET_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"
INCREASE_FAMILY = ("NEW_HOLDER", "LARGE_INCREASE", "CONSECUTIVE_INCREASE", "INCREASE")
EVENT_FIELDS = ("event_id", "security_code", "company_name", "filer_name", "event_type", "tags",
                "disclosed_at", "event_market_date", "source", "source_document_id", "source_url",
                "raw_title", "holding_ratio", "previous_holding_ratio", "holding_change",
                "important_proposal", "is_jpx400_at_event")


def load_filings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""SELECT * FROM filings WHERE security_code IS NOT NULL AND security_code<>''
                           AND submit_datetime IS NOT NULL ORDER BY submit_datetime, doc_id""")
    return [dict(r) for r in rows]


def classify(filing: dict, prior_increase_dates: list[date], disclosed: date, cfg) -> tuple[str, list[str]]:
    change = filing.get("holding_change")
    important = bool(filing.get("important_proposal"))
    tags: list[str] = []
    if filing.get("report_type") == "correction":
        etype = "CORRECTION"
        tags = ["CORRECTION"]
    elif filing.get("report_type") == "initial":
        etype = "NEW_HOLDER"
        tags = ["NEW_HOLDER"]
    elif change is None or change == 0:
        etype = "CHANGE_UNKNOWN"
    elif change < 0:
        etype = "DECREASE"
        tags = ["DECREASE"]
    else:
        window = cfg.events.consecutive_window_days
        consecutive = any(0 <= (disclosed - prior).days <= window for prior in prior_increase_dates)
        large = change >= cfg.events.large_increase_pt
        tags = ["INCREASE"]
        if large:
            tags.append("LARGE_INCREASE")
        if consecutive:
            tags.append("CONSECUTIVE_INCREASE")
        etype = "CONSECUTIVE_INCREASE" if consecutive else ("LARGE_INCREASE" if large else "INCREASE")
    if important:
        tags.append("PURPOSE_CHANGE")
    return etype, sorted(set(tags))


def normalize_events(filings: Iterable[dict], calendar: MarketCalendar, cfg, universe=None,
                     codes: set[str] | None = None) -> list[dict]:
    """filings を event_id 昇順の正規化イベントにする。doc_id で重複排除。"""
    by_doc: dict[str, dict] = {}
    for f in filings:
        if f.get("doc_id"):
            by_doc[f["doc_id"]] = f
    ordered = sorted(by_doc.values(), key=lambda f: (f.get("submit_datetime") or "", f["doc_id"]))
    prior_increases: dict[tuple[str, str], list[date]] = {}
    events = []
    for f in ordered:
        code = str(f.get("security_code") or "").strip()
        if not code or (codes is not None and code not in codes):
            continue
        disclosed = to_date(f["submit_datetime"])
        market = calendar.event_market_date(f["submit_datetime"])
        if market is None:
            continue
        key = (f.get("filer_name") or "", code)
        etype, tags = classify(f, prior_increases.get(key, []), disclosed, cfg)
        if f.get("report_type") != "correction" and (f.get("holding_change") or 0) > 0:
            prior_increases.setdefault(key, []).append(disclosed)
        member = bool(universe.is_member(code, market)) if universe is not None else False
        events.append({
            "event_id": f"edinet:{f['doc_id']}", "security_code": code,
            "company_name": f.get("issuer_name") or "", "filer_name": f.get("filer_name") or "",
            "event_type": etype, "tags": tags, "disclosed_at": f["submit_datetime"],
            "event_market_date": market.isoformat(), "source": "EDINET",
            "source_document_id": f["doc_id"], "source_url": EDINET_URL.format(doc_id=f["doc_id"]),
            "raw_title": f.get("doc_description") or "",
            "holding_ratio": f.get("holding_ratio"), "previous_holding_ratio": f.get("previous_holding_ratio"),
            "holding_change": f.get("holding_change"),
            "important_proposal": bool(f.get("important_proposal")), "is_jpx400_at_event": member,
        })
    events.sort(key=lambda e: e["event_id"])
    return events


def in_window(events: list[dict], start: str) -> list[dict]:
    """disclosed_at が start(ISO) 以降のイベント。"""
    return [e for e in events if e["disclosed_at"][:10] >= start]


def events_by_month(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in events:
        out.setdefault(e["disclosed_at"][:7], []).append(e)
    return {k: sorted(v, key=lambda e: e["event_id"]) for k, v in sorted(out.items())}
