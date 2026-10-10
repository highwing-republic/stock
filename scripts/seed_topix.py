"""TOPIX ユニバースの初回シード。

JPX 総研「TOPIX初回定期入替における選定結果及び構成銘柄一覧」PDF から
state/topix/membership.json（= 構成銘柄 − 移行措置銘柄 = 986 銘柄）、
state/topix/transition.json（移行措置 683 銘柄の除外リスト）、
state/topix/seed_2026.json（解析結果の記録）を生成する。

python scripts/seed_topix.py --pdf topix_j.pdf [--state-dir state/topix] [--config config/watch-topix.yaml] [--force]
--pdf を省略すると JPX から取得する。membership.json が既にあれば --force が無い限り何もしない。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.watch.config import load_config  # noqa: E402
from src.watch.sources import topix  # noqa: E402
from src.watch.store import write_json  # noqa: E402
from src.watch.universe import SCHEMA_VERSION, dumps_membership, save_membership  # noqa: E402

JST = timezone(timedelta(hours=9))
EXPECTED_MEMBERS = 986
EXPECTED_TRANSITION = 683
TRANSITION_EXCLUDE_UNTIL = "2028-07-31"  # 8 回の段階的ウエイト低減（2026-10〜2028-07）の最終回


def build_membership(sel: topix.SelectionResult, *, continuing_from: date, added_from: date,
                     checked_at: datetime) -> dict:
    added = {c for c, _, _ in sel.added}
    members = []
    for code, name, market in sel.members():
        members.append({
            "security_code": code,
            "company_name": name,
            "effective_from": (added_from if code in added else continuing_from).isoformat(),
            "effective_to": None,
            "source_url": sel.source_url,
            "source_published_at": sel.published_at.isoformat(),
            "seeded": True,
            "market_segment": market,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "last_checked_at": checked_at.isoformat(timespec="seconds"),
        "last_source_sha256": sel.sha256,
        "last_as_of": sel.published_at.isoformat(),
        "members": members,
    }


def build_transition(sel: topix.SelectionResult) -> dict:
    return {
        "schema_version": 1,
        "source_url": sel.source_url,
        "published_at": sel.published_at.isoformat(),
        "exclude_until": TRANSITION_EXCLUDE_UNTIL,
        "note": "移行措置銘柄。2026-10-30 から 2028-07 末まで 8 回に分けてウエイトが低減される。本ユニバースでは最初から対象外。",
        "codes": [{"security_code": c, "company_name": n, "market_segment": m} for c, n, m in sel.transition],
    }


def build_seed_record(sel: topix.SelectionResult) -> dict:
    rows = lambda items: [{"security_code": c, "company_name": n, "market_segment": m} for c, n, m in items]  # noqa: E731
    return {
        "schema_version": 1,
        "source_url": sel.source_url,
        "sha256": sel.sha256,
        "published_at": sel.published_at.isoformat(),
        "effective_at": sel.effective_at.isoformat() if sel.effective_at else None,
        "counts": {"added": len(sel.added), "transition": len(sel.transition),
                   "constituents": len(sel.constituents), "members": len(sel.members())},
        "added": rows(sel.added),
        "transition": rows(sel.transition),
        "constituents": rows(sel.constituents),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TOPIX ユニバースを選定結果 PDF からシードする")
    parser.add_argument("--pdf", help="選定結果 PDF のパス（省略時は JPX から取得）")
    parser.add_argument("--state-dir", default="state/topix")
    parser.add_argument("--config", default="config/watch-topix.yaml")
    parser.add_argument("--force", action="store_true", help="既存の membership.json を上書きする")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(ROOT / args.config)
    state_dir = Path(args.state_dir)
    state_dir = state_dir if state_dir.is_absolute() else ROOT / state_dir
    membership_path = state_dir / "membership.json"
    if membership_path.exists() and not args.force:
        logging.error("%s が既にあります。上書きするなら --force", membership_path)
        return 1

    if args.pdf:
        sel = topix.parse_selection_pdf(Path(args.pdf).read_bytes())
    else:
        sel = topix.fetch_selection()
    members = sel.members()
    if len(members) != EXPECTED_MEMBERS or len(sel.transition) != EXPECTED_TRANSITION:
        logging.error("件数が想定と違います: members=%d (期待 %d), transition=%d (期待 %d)。シードを中止",
                      len(members), EXPECTED_MEMBERS, len(sel.transition), EXPECTED_TRANSITION)
        return 1

    now = datetime.now(JST)
    membership = build_membership(
        sel, continuing_from=date.fromisoformat(str(cfg.universe.continuing_effective_from)),
        added_from=date.fromisoformat(str(cfg.universe.added_effective_from)), checked_at=now)
    save_membership(membership_path, membership)
    write_json(state_dir / cfg.universe.transition_file, build_transition(sel))
    write_json(state_dir / "seed_2026.json", build_seed_record(sel))
    logging.info("seeded %s: members=%d transition=%d (published %s, effective %s)",
                 state_dir, len(members), len(sel.transition), sel.published_at, sel.effective_at)
    assert dumps_membership(membership)  # 決定的に直列化できることの確認
    return 0


if __name__ == "__main__":
    sys.exit(main())
