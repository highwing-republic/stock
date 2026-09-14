from __future__ import annotations
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import api_key
from src.radar import EdinetClient, connect, generate_json, sync_date, update_issuer_master, update_prices

def main():
    parser = argparse.ArgumentParser(description="EDINET投資レーダーを更新")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--skip-master", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = api_key()
    if not key:
        raise SystemExit("EDINET_API_KEY is not set")
    conn = connect()
    if not args.skip_master:
        logging.info("issuer master: %s rows", update_issuer_master(conn))
    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=max(0, args.days - 1))
    client = EdinetClient(key)
    current = start
    while current <= end:
        try:
            logging.info("EDINET %s: %s filings", current, sync_date(conn, client, current))
        except Exception:
            logging.exception("EDINET date failed: %s", current)
        current += timedelta(days=1)
    price_count, failures = update_prices(conn)
    logging.info("prices: %s rows", price_count)
    for failure in failures:
        logging.warning("price failure: %s", failure)
    logging.info("json: %s", generate_json(conn))

if __name__ == "__main__":
    main()
