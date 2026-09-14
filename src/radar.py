from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .config import API_BASE, BENCHMARK_LABEL, BENCHMARK_TICKER, CODE_LIST_URL, ROOT, api_key, db_path

LOG = logging.getLogger("edinet_radar")

SCHEMA = """
CREATE TABLE IF NOT EXISTS issuer_master(
 edinet_code TEXT PRIMARY KEY, company_name TEXT, security_code TEXT, ticker TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS filings(
 doc_id TEXT PRIMARY KEY, submit_datetime TEXT, report_type TEXT, doc_type_code TEXT,
 filer_name TEXT, issuer_edinet_code TEXT, issuer_name TEXT, security_code TEXT, ticker TEXT,
 holding_ratio REAL, previous_holding_ratio REAL, holding_change REAL, holding_shares REAL,
 purpose TEXT, joint_holders TEXT, important_proposal INTEGER, doc_description TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS prices(
 ticker TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
 adj_close REAL, volume INTEGER, PRIMARY KEY(ticker,trade_date));
CREATE TABLE IF NOT EXISTS sync_history(
 target_date TEXT PRIMARY KEY, status TEXT, documents_count INTEGER, filings_count INTEGER,
 error_message TEXT, synced_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_filings_submit ON filings(submit_datetime DESC);
CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON prices(ticker,trade_date);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def security_code_to_ticker(value: object) -> tuple[str | None, str | None]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 4:
        return None, None
    code = digits[:4]
    return code, f"{code}.T"


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("cp932", "utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_code_list_zip(content: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = next((x for x in archive.namelist() if x.lower().endswith(".csv")), None)
        if not name:
            raise ValueError("EDINET code list ZIP has no CSV")
        reader = csv.DictReader(io.StringIO(_decode(archive.read(name))))
        items = []
        for row in reader:
            clean = {re.sub(r"[\s　]", "", k or ""): str(v or "").strip() for k, v in row.items()}
            code = next((v for k, v in clean.items() if "EDINETコード" in k or "ＥＤＩＮＥＴコード" in k), "")
            company = next((v for k, v in clean.items() if "提出者名" in k), "")
            sec = next((v for k, v in clean.items() if "証券コード" in k), "")
            security_code, ticker = security_code_to_ticker(sec)
            if code:
                items.append({"edinet_code": code, "company_name": company,
                              "security_code": security_code, "ticker": ticker})
        return items


class EdinetClient:
    def __init__(self, key: str, session=None, retries: int = 3):
        if not key:
            raise ValueError("EDINET_API_KEY is required")
        self.key, self.session, self.retries = key, session or requests.Session(), retries

    def _get(self, path: str, params: dict[str, Any]):
        last = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(f"{API_BASE}/{path}",
                    params={**params, "Subscription-Key": self.key}, timeout=(10, 60))
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"EDINET request failed: {last}") from last

    def list_documents(self, target_date: str) -> list[dict[str, Any]]:
        return self._get("documents.json", {"date": target_date, "type": 2}).json().get("results") or []

    def download_csv(self, doc_id: str) -> bytes:
        return self._get(f"documents/{doc_id}", {"type": 5}).content


def classify_report(doc: dict[str, Any]) -> str:
    text = str(doc.get("docDescription") or "")
    if "訂正" in text:
        return "correction"
    if "変更報告書" in text:
        return "change"
    if "大量保有報告書" in text:
        return "initial"
    return "unknown"


def is_large_holding_document(doc: dict[str, Any]) -> bool:
    text = str(doc.get("docDescription") or "")
    return "大量保有報告書" in text or "変更報告書" in text


def _number(value: object, percentage: bool = False) -> float | None:
    text = str(value or "").replace(",", "").replace("％", "").replace("%", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    if percentage and abs(number) <= 1:
        number *= 100
    return round(number, 4)


def parse_filing_csv_zip(content: bytes) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith((".csv", ".tsv")):
                continue
            text = _decode(archive.read(name))
            first = text.splitlines()[0] if text.splitlines() else ""
            rows.extend(csv.DictReader(io.StringIO(text), delimiter="\t" if "\t" in first else ","))
    result = {"holding_ratio": None, "previous_holding_ratio": None, "holding_change": None,
              "holding_shares": None, "purpose": None, "joint_holders": None,
              "important_proposal": None}
    current, previous, purposes, holders = [], [], [], []
    for row in rows:
        values = list(row.values())
        keys = list(row.keys())
        pick = lambda words: next((str(v or "") for k, v in row.items()
                                  if any(w.lower() in re.sub(r"[\s　]", "", k or "").lower() for w in words)), "")
        element = pick(("要素id", "elementid", "要素名"))
        label = pick(("項目名", "ラベル", "itemname"))
        context = pick(("コンテキストid", "contextid"))
        value = pick(("値", "value"))
        hay = f"{element} {label} {context}".lower()
        if not value.strip():
            continue
        ratio = "保有割合" in label or any(x in element.lower() for x in ("ratioofshareholding", "holdingratio", "shareholdingratio"))
        prior = any(x in hay for x in ("直前", "前回", "former", "prior", "previous"))
        if ratio:
            parsed = _number(value, True)
            if parsed is not None:
                (previous if prior else current).append(parsed)
        elif "保有目的" in label or "purposeofholding" in element.lower():
            purposes.append(value.strip())
        elif "共同保有者" in label and ("氏名" in label or "名称" in label):
            holders.append(value.strip())
        elif "重要提案行為" in label or "importantproposal" in element.lower():
            result["important_proposal"] = int(any(x in value for x in ("有", "あり", "該当", "Yes")))
        elif "保有株券等の数" in label or "numberofsharesheld" in element.lower():
            result["holding_shares"] = _number(value)
    result["holding_ratio"] = current[-1] if current else None
    result["previous_holding_ratio"] = previous[-1] if previous else None
    if result["holding_ratio"] is not None and result["previous_holding_ratio"] is not None:
        result["holding_change"] = round(result["holding_ratio"] - result["previous_holding_ratio"], 4)
    result["purpose"] = " / ".join(dict.fromkeys(purposes))[:1000] or None
    result["joint_holders"] = " / ".join(dict.fromkeys(holders))[:1000] or None
    return result


def update_issuer_master(conn: sqlite3.Connection, session=None) -> int:
    response = (session or requests).get(CODE_LIST_URL, timeout=(10, 60))
    response.raise_for_status()
    rows = parse_code_list_zip(response.content)
    conn.executemany("""INSERT INTO issuer_master(edinet_code,company_name,security_code,ticker,updated_at)
      VALUES(:edinet_code,:company_name,:security_code,:ticker,CURRENT_TIMESTAMP)
      ON CONFLICT(edinet_code) DO UPDATE SET company_name=excluded.company_name,
      security_code=excluded.security_code,ticker=excluded.ticker,updated_at=CURRENT_TIMESTAMP""", rows)
    conn.commit()
    return len(rows)


def sync_date(conn: sqlite3.Connection, client: EdinetClient, target: date) -> int:
    day = target.isoformat()
    documents = client.list_documents(day)
    count = 0
    errors = []
    for doc in documents:
        if not is_large_holding_document(doc):
            continue
        try:
            issuer_code = doc.get("issuerEdinetCode")
            master = conn.execute("SELECT * FROM issuer_master WHERE edinet_code=?", (issuer_code,)).fetchone()
            parsed = {}
            if int(doc.get("csvFlag") or 0) == 1:
                parsed = parse_filing_csv_zip(client.download_csv(doc["docID"]))
            row = {"doc_id": doc.get("docID"), "submit_datetime": doc.get("submitDateTime"),
                   "report_type": classify_report(doc), "doc_type_code": doc.get("docTypeCode"),
                   "filer_name": doc.get("filerName"), "issuer_edinet_code": issuer_code,
                   "issuer_name": master["company_name"] if master else None,
                   "security_code": master["security_code"] if master else None,
                   "ticker": master["ticker"] if master else None,
                   "doc_description": doc.get("docDescription"), **parsed}
            fields = tuple(row)
            conn.execute(f"""INSERT INTO filings({','.join(fields)}) VALUES({','.join(':'+x for x in fields)})
              ON CONFLICT(doc_id) DO UPDATE SET {','.join(x+'=excluded.'+x for x in fields if x != 'doc_id')},updated_at=CURRENT_TIMESTAMP""", row)
            count += 1
        except Exception as exc:  # keep partial batch alive
            errors.append(f"{doc.get('docID')}: {exc}")
            LOG.exception("filing failed doc_id=%s", doc.get("docID"))
    conn.execute("""INSERT INTO sync_history(target_date,status,documents_count,filings_count,error_message,synced_at)
      VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(target_date) DO UPDATE SET status=excluded.status,
      documents_count=excluded.documents_count,filings_count=excluded.filings_count,
      error_message=excluded.error_message,synced_at=CURRENT_TIMESTAMP""",
      (day, "partial" if errors else "ok", len(documents), count, "\n".join(errors)[:4000] or None))
    conn.commit()
    return count


def update_prices(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    import yfinance as yf
    tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM filings WHERE ticker IS NOT NULL")]
    tickers = sorted(set(tickers + [BENCHMARK_TICKER]))
    failures, total = [], 0
    end = (date.today() + timedelta(days=1)).isoformat()
    for ticker in tickers:
        try:
            latest = conn.execute("SELECT MAX(trade_date) FROM prices WHERE ticker=?", (ticker,)).fetchone()[0]
            start = (date.fromisoformat(latest) - timedelta(days=5)).isoformat() if latest else (date.today() - timedelta(days=400)).isoformat()
            frame = yf.download(ticker, start=start, end=end, interval="1d", auto_adjust=False,
                                actions=False, progress=False, threads=False)
            if getattr(frame.columns, "nlevels", 1) > 1:
                frame.columns = frame.columns.get_level_values(0)
            rows = []
            for index, item in frame.iterrows():
                get = lambda key: None if key not in item or item[key] != item[key] else float(item[key])
                volume = get("Volume")
                rows.append({"ticker": ticker, "trade_date": index.strftime("%Y-%m-%d"),
                    "open": get("Open"), "high": get("High"), "low": get("Low"), "close": get("Close"),
                    "adj_close": get("Adj Close") or get("Close"), "volume": int(volume) if volume is not None else None})
            conn.executemany("""INSERT INTO prices VALUES(:ticker,:trade_date,:open,:high,:low,:close,:adj_close,:volume)
              ON CONFLICT(ticker,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,
              close=excluded.close,adj_close=excluded.adj_close,volume=excluded.volume""", rows)
            conn.commit(); total += len(rows)
        except Exception as exc:
            failures.append(f"{ticker}: {exc}"); LOG.exception("price failed ticker=%s", ticker)
    return total, failures


def event_metrics(submit_datetime: str, stock: list[dict], benchmark: list[dict]) -> dict[str, Any]:
    event = submit_datetime[:10]
    stock = sorted((x for x in stock if x.get("adj_close") is not None), key=lambda x: x["trade_date"])
    before, after = [x for x in stock if x["trade_date"] < event], [x for x in stock if x["trade_date"] > event]
    empty = {k: None for k in ("return1d","return5d","return20d","returnCurrent","volumeRatio","marketExcessReturn")}
    if not before: return empty
    base = float(before[-1]["adj_close"])
    change = lambda value: round((float(value) / base - 1) * 100, 2) if value is not None and base else None
    at = lambda n: after[n]["adj_close"] if len(after) > n else None
    result = {"return1d": change(at(0)), "return5d": change(at(4)), "return20d": change(at(19)),
              "returnCurrent": change(stock[-1]["adj_close"]), "volumeRatio": None, "marketExcessReturn": None}
    pre_vol = [float(x["volume"]) for x in before[-20:] if x.get("volume") is not None]
    post_vol = [float(x["volume"]) for x in after[:5] if x.get("volume") is not None]
    if pre_vol and post_vol and sum(pre_vol):
        result["volumeRatio"] = round((sum(post_vol)/len(post_vol))/(sum(pre_vol)/len(pre_vol)), 2)
    bbefore = [x for x in benchmark if x["trade_date"] < event and x.get("adj_close") is not None]
    bafter = [x for x in benchmark if x["trade_date"] > event and x.get("adj_close") is not None]
    if bbefore and bafter and result["return5d"] is not None:
        target = bafter[min(4, len(bafter)-1)]["adj_close"]
        breturn = (float(target)/float(bbefore[-1]["adj_close"])-1)*100
        result["marketExcessReturn"] = round(result["return5d"]-breturn, 2)
    return result


def signal(report_type, change, important, volume_ratio, excess, return_5d):
    score, reasons = 0, []
    def add(points, label):
        nonlocal score; score += points; reasons.append(label)
    if report_type == "initial": add(30, "新規大量保有")
    if change is not None and change >= 1: add(20, "1pt以上買い増し")
    if change is not None and change >= 2: add(10, "2pt以上買い増し")
    if important: add(20, "重要提案行為あり")
    if volume_ratio is not None and volume_ratio >= 2: add(10, "出来高2倍以上")
    if volume_ratio is not None and volume_ratio >= 3: add(5, "出来高3倍以上")
    unreacted = (report_type == "initial" or (change is not None and change >= 1)) and return_5d is not None and return_5d < 3
    if unreacted: add(10, "株価未反応")
    if excess is not None and excess >= 5: add(10, "市場超過5%以上")
    if change is not None and change < 0: add(-20, "保有割合減少")
    if change is not None and change <= -2: add(-20, "大幅売却")
    return max(0, min(100, score)), reasons, unreacted


def _price_rows(conn, ticker):
    return [dict(x) for x in conn.execute("SELECT * FROM prices WHERE ticker=? ORDER BY trade_date", (ticker,))]


def generate_json(conn: sqlite3.Connection, out_dir: Path | None = None) -> dict[str, int]:
    out = out_dir or ROOT / "public" / "data"
    stocks_dir = out / "stocks"; stocks_dir.mkdir(parents=True, exist_ok=True)
    benchmark = _price_rows(conn, BENCHMARK_TICKER)
    filings = [dict(x) for x in conn.execute("SELECT * FROM filings ORDER BY submit_datetime DESC")]
    items, grouped = [], {}
    for filing in filings:
        prices = _price_rows(conn, filing["ticker"]) if filing.get("ticker") else []
        metrics = event_metrics(filing["submit_datetime"] or "", prices, benchmark)
        score, reasons, unreacted = signal(filing["report_type"], filing["holding_change"],
            bool(filing["important_proposal"]), metrics["volumeRatio"], metrics["marketExcessReturn"], metrics["return5d"])
        item = {"docId": filing["doc_id"], "ticker": filing["ticker"], "securityCode": filing["security_code"],
            "companyName": filing["issuer_name"] or "対象会社名未取得", "submitDatetime": filing["submit_datetime"],
            "filerName": filing["filer_name"], "reportType": filing["report_type"],
            "holdingRatio": filing["holding_ratio"], "previousHoldingRatio": filing["previous_holding_ratio"],
            "holdingChange": filing["holding_change"], "purpose": filing["purpose"],
            "importantProposal": bool(filing["important_proposal"]), "priceChange": metrics["return5d"],
            "volumeRatio": metrics["volumeRatio"], "marketExcessReturn": metrics["marketExcessReturn"],
            "signalScore": score, "signalReasons": reasons, "stockUnreacted": unreacted,
            "metrics": metrics}
        items.append(item)
        if filing.get("security_code"): grouped.setdefault(filing["security_code"], []).append(item)
    now = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    today = now[:10]
    kpis = {"today": sum((x["submitDatetime"] or "")[:10] == today for x in items),
            "initial": sum(x["reportType"] == "initial" for x in items if (x["submitDatetime"] or "")[:10] == today),
            "increase": sum((x["holdingChange"] or 0) > 0 for x in items if (x["submitDatetime"] or "")[:10] == today),
            "unreacted": sum(x["stockUnreacted"] for x in items)}
    _write_json(out / "latest.json", {"updatedAt": now, "kpis": kpis, "items": items})
    _write_json(out / "signals.json", {"updatedAt": now, "items": [x for x in items if x["signalScore"] > 0]})
    for code, events in grouped.items():
        ticker = events[0]["ticker"]
        _write_json(stocks_dir / f"{code}.json", {"updatedAt": now,
            "company": {"securityCode": code, "ticker": ticker, "name": events[0]["companyName"]},
            "events": events, "prices": _price_rows(conn, ticker), "benchmark": benchmark,
            "benchmarkLabel": BENCHMARK_LABEL})
    sync = conn.execute("SELECT COUNT(*),MAX(synced_at) FROM sync_history WHERE status IN ('ok','partial')").fetchone()
    _write_json(out / "metadata.json", {"status": "ready" if items else "no_filings", "updatedAt": now,
        "filingCount": len(items), "stockCount": len(grouped), "syncedDays": sync[0],
        "lastSyncAt": sync[1], "benchmark": {"ticker": BENCHMARK_TICKER, "label": BENCHMARK_LABEL},
        "sources": ["金融庁 EDINET API v2", "Yahoo Finance / yfinance"]})
    return {"filings": len(items), "stocks": len(grouped)}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temp.replace(path)
