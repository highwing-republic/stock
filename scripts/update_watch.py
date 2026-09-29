"""JPX400 Watch Radar の更新 CLI。

python scripts/update_watch.py [--as-of YYYY-MM-DD] [--offline]
環境変数: DB_PATH（既定 data/investment_radar.db）, WATCH_STATE_DIR（既定 state）, WATCH_OUT_DIR（既定 public/data/watch）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config import db_path  # noqa: E402
from src.watch import pipeline  # noqa: E402
from src.watch.config import load_config  # noqa: E402


def _dir(value: str | None, env: str, default: str) -> Path:
    path = Path(value or os.getenv(env) or default)
    return path if path.is_absolute() else ROOT / path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="JPX400 Watch Radar を更新")
    parser.add_argument("--as-of", help="基準日 YYYY-MM-DD（既定: 今日 JST）")
    parser.add_argument("--offline", action="store_true", help="ネットワーク取得をせず DB と state だけで再計算")
    parser.add_argument("--state-dir")
    parser.add_argument("--out-dir")
    parser.add_argument("--db")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = pipeline.run(
        db_path=Path(args.db) if args.db else db_path(),
        state_dir=_dir(args.state_dir, "WATCH_STATE_DIR", "state"),
        out_dir=_dir(args.out_dir, "WATCH_OUT_DIR", "public/data/watch"),
        cfg=load_config(args.config), as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        offline=args.offline)
    logging.info("watch: %s (%s) %s", result.status, result.message, result.details)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
