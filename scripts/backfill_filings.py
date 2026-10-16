"""検証用の一回限りのバックフィル: 期間内の大量保有報告・変更報告のうち、新TOPIX 構成銘柄が対象のものだけを CSV.gz に書く。

本番の DB・公開 JSON には触れない。EDINET の取得・解析は src/radar.py と同じ関数を使う。

python scripts/backfill_filings.py --start 2022-01-01 --end 2022-06-30 --out backfill_out/filings_2022-01-01.csv.gz
環境変数 EDINET_API_KEY が必要。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config import CODE_LIST_URL, api_key  # noqa: E402
from src.radar import (EdinetClient, classify_report, is_large_holding_document,  # noqa: E402
                       parse_code_list_zip, parse_filing_csv_zip)

FIELDS = ["doc_id", "submit_datetime", "report_type", "doc_type_code", "filer_name", "issuer_edinet_code",
          "security_code", "doc_description", "csv_flag", "holding_ratio", "previous_holding_ratio",
          "holding_change", "holding_shares", "purpose", "joint_holders", "important_proposal", "parse_error"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--universe", default=str(ROOT / "state" / "topix" / "topix_membership.json"))
    ap.add_argument("--sleep", type=float, default=0.15, help="書類ダウンロード間の待ち秒数")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = api_key()
    if not key:
        raise SystemExit("EDINET_API_KEY is not set")

    universe = {m["security_code"] for m in json.loads(Path(args.universe).read_text(encoding="utf-8"))["members"]}
    resp = requests.get(CODE_LIST_URL, timeout=(10, 120))
    resp.raise_for_status()
    master = {r["edinet_code"]: r["security_code"] for r in parse_code_list_zip(resp.content) if r["security_code"]}
    logging.info("universe=%d issuer_master=%d", len(universe), len(master))

    client = EdinetClient(key)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    failed_days: list[str] = []
    zero_weekdays: list[str] = []
    totals = {"days": 0, "docs": 0, "large": 0, "in_universe": 0, "parse_errors": 0, "throttle_waits": 0}
    waits = [60, 120, 300, 600, 900]

    def list_documents_checked(day: date) -> list:
        """書類一覧を取得。metadata.status が 200 でない、または平日に 0 件なら制限とみなして待って再試行する。"""
        for attempt in range(len(waits) + 1):
            problem = None
            try:
                resp = client._get("documents.json", {"date": day.isoformat(), "type": 2})
                body = resp.json()
                status = str((body.get("metadata") or {}).get("status") or body.get("StatusCode") or "")
                results = body.get("results") or []
                if status != "200":
                    problem = f"metadata status={status!r} body={str(body)[:300]}"
                elif not results and day.weekday() < 5:
                    problem = "weekday returned 0 documents"
                else:
                    return results
            except Exception as exc:
                problem = f"error {exc}"[:300]
            if attempt == len(waits):
                if problem == "weekday returned 0 documents":
                    zero_weekdays.append(day.isoformat())  # 祝日の可能性。記録して先へ進む
                    return []
                raise RuntimeError(problem)
            totals["throttle_waits"] += 1
            logging.warning("%s: %s; waiting %ss", day, problem, waits[attempt])
            time.sleep(waits[attempt])
        return []

    def process_day(writer, day: date) -> None:
        docs = list_documents_checked(day)
        totals["docs"] += len(docs)
        for doc in docs:
            if not is_large_holding_document(doc):
                continue
            totals["large"] += 1
            sec = master.get(doc.get("issuerEdinetCode"))
            if sec not in universe:
                continue
            totals["in_universe"] += 1
            row = {"doc_id": doc.get("docID"), "submit_datetime": doc.get("submitDateTime"),
                   "report_type": classify_report(doc), "doc_type_code": doc.get("docTypeCode"),
                   "filer_name": doc.get("filerName"), "issuer_edinet_code": doc.get("issuerEdinetCode"),
                   "security_code": sec, "doc_description": doc.get("docDescription"),
                   "csv_flag": doc.get("csvFlag"), "parse_error": None}
            if int(doc.get("csvFlag") or 0) == 1:
                for attempt in range(3):
                    try:
                        row.update(parse_filing_csv_zip(client.download_csv(doc["docID"])))
                        row["parse_error"] = None
                        break
                    except Exception as exc:  # 制限中は ZIP でない応答が返る。待って取り直す
                        row["parse_error"] = str(exc)[:200]
                        if attempt < 2:
                            totals["throttle_waits"] += 1
                            time.sleep(30 * (attempt + 1))
                if row["parse_error"]:
                    totals["parse_errors"] += 1
                time.sleep(args.sleep)
            writer.writerow({k: row.get(k) for k in FIELDS})

    with gzip.open(out, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        day = start
        while day <= end:
            try:
                process_day(writer, day)
                totals["days"] += 1
            except Exception:
                logging.exception("day failed: %s", day)
                failed_days.append(day.isoformat())
            if day.day == 1 or day == end:
                logging.info("progress %s %s", day, totals)
            day += timedelta(days=1)
        for iso in list(failed_days):  # 失敗した日をもう一度だけ
            try:
                time.sleep(5)
                process_day(writer, date.fromisoformat(iso))
                failed_days.remove(iso)
                totals["days"] += 1
            except Exception:
                logging.exception("retry failed: %s", iso)
    summary = dict(totals, start=args.start, end=args.end, failed_days=failed_days, zero_weekdays=zero_weekdays)
    out.with_suffix("").with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                                encoding="utf-8")
    logging.info("done %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
