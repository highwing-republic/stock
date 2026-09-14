from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
CODE_LIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
BENCHMARK_TICKER = "1306.T"
BENCHMARK_LABEL = "TOPIX連動ETF（1306）"

def db_path() -> Path:
    path = Path(os.getenv("DB_PATH", "data/investment_radar.db"))
    return path if path.is_absolute() else ROOT / path

def api_key() -> str:
    return os.getenv("EDINET_API_KEY", "").strip()
