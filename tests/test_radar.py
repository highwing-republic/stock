import csv, io, zipfile
from datetime import date, timedelta
from pathlib import Path

from src.radar import (classify_report, connect, event_metrics, generate_json,
                       is_large_holding_document, parse_code_list_zip,
                       parse_filing_csv_zip, security_code_to_ticker, signal)

def zipped(name, text, encoding="utf-8-sig"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text.encode(encoding))
    return buffer.getvalue()

def test_classification_and_filter():
    assert classify_report({"docDescription":"変更報告書No.2"}) == "change"
    assert classify_report({"docDescription":"訂正大量保有報告書"}) == "correction"
    assert is_large_holding_document({"docDescription":"大量保有報告書"})

def test_mapper_uses_first_four_digits():
    assert security_code_to_ticker("72030") == ("7203", "7203.T")
    content = zipped("EdinetcodeDlInfo.csv", "ＥＤＩＮＥＴコード,提出者名,証券コード\nE02144,トヨタ自動車株式会社,72030\n", "cp932")
    assert parse_code_list_zip(content)[0]["ticker"] == "7203.T"

def test_csv_parser_keeps_partial_values():
    text = "要素ID\t項目名\tコンテキストID\t値\n" \
           "jplvh:RatioOfShareholding\t株券等保有割合\tCurrent\t0.074\n" \
           "jplvh:RatioOfShareholding\t直前の株券等保有割合\tPrior\t0.052\n" \
           "jplvh:PurposeOfHolding\t保有目的\tCurrent\t純投資\n"
    parsed = parse_filing_csv_zip(zipped("data.tsv", text, "utf-16"))
    assert parsed["holding_ratio"] == 7.4
    assert parsed["previous_holding_ratio"] == 5.2
    assert parsed["holding_change"] == 2.2
    assert parsed["purpose"] == "純投資"

def test_metrics_and_signal():
    start = date(2026, 1, 1)
    prices = [{"trade_date":(start+timedelta(days=i)).isoformat(), "adj_close":100+i, "volume":100} for i in range(35)]
    bench = [{"trade_date":x["trade_date"], "adj_close":100, "volume":100} for x in prices]
    metrics = event_metrics("2026-01-11T10:00:00", prices, bench)
    assert metrics["return5d"] == 4.59
    assert metrics["volumeRatio"] == 1.0
    score, reasons, unreacted = signal("initial", 2.0, False, 3.0, 6.0, 2.0)
    assert score == 95 and unreacted and "株価未反応" in reasons

def test_export_structure(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.execute("""INSERT INTO filings(doc_id,submit_datetime,report_type,filer_name,
      issuer_name,security_code,ticker,holding_ratio) VALUES('S1','2026-01-10T10:00:00','initial',
      '投資家','テスト社','1234','1234.T',5.2)""")
    conn.commit()
    output = tmp_path / "public"
    result = generate_json(conn, output)
    assert result == {"filings":1,"stocks":1}
    assert (output / "latest.json").exists()
    assert (output / "stocks" / "1234.json").exists()
