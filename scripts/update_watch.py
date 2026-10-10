"""Watch Radar（JPX400 / TOPIX）の更新 CLI。

python scripts/update_watch.py [--universe jpx400|topix] [--as-of YYYY-MM-DD] [--offline]
環境変数: DB_PATH（既定 data/investment_radar.db）, WATCH_STATE_DIR, WATCH_OUT_DIR, WATCH_CONFIG
--universe の既定は jpx400（従来どおり state/ と public/data/watch/）。
--universe topix は config/watch-topix.yaml, state/topix/, public/data/watch-topix/ を既定にする。
明示した --config / --state-dir / --out-dir / 環境変数があればそちらを優先する。
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


UNIVERSE_DEFAULTS = {
    "jpx400": {"config": None, "state": "state", "out": "public/data/watch"},
    "topix": {"config": "config/watch-topix.yaml", "state": "state/topix", "out": "public/data/watch-topix"},
}


def _dir(value: str | None, env: str, default: str) -> Path:
    path = Path(value or os.getenv(env) or default)
    return path if path.is_absolute() else ROOT / path


def resolve_paths(universe: str, *, config=None, state_dir=None, out_dir=None) -> dict:
    """--universe に応じた既定パス。明示指定・環境変数が優先。"""
    d = UNIVERSE_DEFAULTS[universe]
    cfg_path = config or os.getenv("WATCH_CONFIG") or (str(ROOT / d["config"]) if d["config"] else None)
    return {"config": cfg_path,
            "state_dir": _dir(state_dir, "WATCH_STATE_DIR", d["state"]),
            "out_dir": _dir(out_dir, "WATCH_OUT_DIR", d["out"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Watch Radar（JPX400 / TOPIX）を更新")
    parser.add_argument("--universe", choices=sorted(UNIVERSE_DEFAULTS), default="jpx400",
                        help="監視ユニバース（既定: jpx400）")
    parser.add_argument("--as-of", help="基準日 YYYY-MM-DD（既定: 今日 JST）")
    parser.add_argument("--offline", action="store_true", help="ネットワーク取得をせず DB と state だけで再計算")
    parser.add_argument("--state-dir")
    parser.add_argument("--out-dir")
    parser.add_argument("--db")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths = resolve_paths(args.universe, config=args.config, state_dir=args.state_dir, out_dir=args.out_dir)
    cfg = load_config(paths["config"])
    if cfg.universe.name != args.universe:
        parser.error(f"--universe {args.universe} but config universe.name is {cfg.universe.name}")
    result = pipeline.run(
        db_path=Path(args.db) if args.db else db_path(),
        state_dir=paths["state_dir"], out_dir=paths["out_dir"],
        cfg=cfg, as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        offline=args.offline)
    logging.info("watch: %s (%s) %s", result.status, result.message, result.details)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
