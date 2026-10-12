"""新TOPIX Watch Radar の初回シード（一回限り）。

JPX総研 2026-10-07 公表「TOPIX初回定期入替における選定結果及び構成銘柄一覧」PDF から
state/topix/topix_membership.json・topix_adjustments.json・topix_rebalance_2026.json を作る。

python scripts/seed_topix.py [--pdf topix_j.pdf] [--weight-csv topixweight_j.csv] [--codelist Edinetcode.zip]
                             [--state-dir state/topix] [--include-transition] [--force]

銘柄名は (1) ウエイト一覧 CSV（指定が無ければ JPX から取得）→ (2) EDINET コードリスト ZIP
（指定が無ければ EDINET から取得。API キー不要）→ (3) v1 SQLite の issuer_master の順で引く。
ネットワーク取得はいずれも User-Agent 付きで 1 回ずつ。
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config import CODE_LIST_URL, db_path  # noqa: E402
from src.radar import parse_code_list_zip  # noqa: E402
from src.watch.sources.common import USER_AGENT, normalize_security_code  # noqa: E402
from src.watch.sources import topix  # noqa: E402
from src.watch.store import dumps, write_json  # noqa: E402
from src.watch.universe import save_membership, seed_topix  # noqa: E402

LOG = logging.getLogger("seed_topix")


def _get(url: str, timeout: float = 60) -> bytes:
    import requests

    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response.content


def _names_from_codelist(content: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in parse_code_list_zip(content):
        code = normalize_security_code(item.get("security_code"))
        if code and item.get("company_name") and code not in out:
            out[code] = item["company_name"]
    return out


def _names_from_db(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT security_code, company_name FROM issuer_master WHERE security_code IS NOT NULL")
        return {c: n for c, n in ((normalize_security_code(sc), nm) for sc, nm in rows) if c and nm}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="新TOPIX の membership をシードする")
    parser.add_argument("--pdf", help="定期入替 PDF のローカルパス（無ければ JPX から取得）")
    parser.add_argument("--weight-csv", help="ウエイト一覧 CSV のローカルパス（無ければ JPX から取得）")
    parser.add_argument("--codelist", help="EDINET コードリスト ZIP のローカルパス（無ければ EDINET から取得）")
    parser.add_argument("--state-dir", default="state/topix")
    parser.add_argument("--include-transition", action="store_true", help="移行措置銘柄も対象に含める")
    parser.add_argument("--force", action="store_true", help="既存の membership を上書きする")
    parser.add_argument("--offline", action="store_true", help="ネットワーク取得をしない（ローカルファイルのみ）")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    state = Path(args.state_dir)
    state = state if state.is_absolute() else ROOT / state
    membership_path = state / "topix_membership.json"
    if membership_path.exists() and not args.force:
        LOG.error("%s は既に存在します。上書きするには --force", membership_path)
        return 1

    # ---- 定期入替 PDF
    if args.pdf:
        pdf = Path(args.pdf).read_bytes()
        rebalance = topix.parse_rebalance_pdf(pdf, source_url=topix.REBALANCE_PDF_URL_2026)
    elif args.offline:
        LOG.error("--offline では --pdf が必要です")
        return 1
    else:
        rebalance = topix.fetch_rebalance_pdf()
    LOG.info("PDF: 新規 %d / 移行措置 %d / 構成銘柄 %d", len(rebalance.new), len(rebalance.transition),
             len(rebalance.constituents))

    # ---- 銘柄名
    names: dict[str, str] = {}
    try:
        csv_bytes = Path(args.weight_csv).read_bytes() if args.weight_csv else (None if args.offline else _get(topix.WEIGHT_CSV_URL))
        if csv_bytes:
            snap = topix.parse_weight_csv(csv_bytes, sha256=hashlib.sha256(csv_bytes).hexdigest())
            names.update(dict(snap.rows))
            LOG.info("ウエイト一覧 CSV: %d 件（%s 時点）", len(snap.rows), snap.as_of)
    except Exception as exc:  # 名前の補完は失敗しても続行
        LOG.warning("ウエイト一覧 CSV を使えません: %s", exc)
    missing = [c for c, _ in rebalance.constituents if c not in names]
    if missing:
        try:
            zip_bytes = Path(args.codelist).read_bytes() if args.codelist else (None if args.offline else _get(CODE_LIST_URL))
            if zip_bytes:
                extra = _names_from_codelist(zip_bytes)
                for code in missing:
                    if code in extra:
                        names[code] = extra[code]
                LOG.info("EDINET コードリストで %d 件補完", sum(1 for c in missing if c in names))
        except Exception as exc:
            LOG.warning("EDINET コードリストを使えません: %s", exc)
    missing = [c for c, _ in rebalance.constituents if c not in names]
    if missing:
        for code, name in _names_from_db(db_path()).items():
            if code in missing:
                names[code] = name

    # ---- membership
    membership, report = seed_topix(rebalance, names, include_transition=args.include_transition)
    LOG.info("membership: %s", {k: v for k, v in report.items() if k != "unnamed"})
    if report["unnamed"]:
        LOG.warning("銘柄名が見つからず証券コードを仮の名前にした銘柄: %s", report["unnamed"])
    state.mkdir(parents=True, exist_ok=True)
    save_membership(membership_path, membership)
    adjustments_path = state / "topix_adjustments.json"
    if not adjustments_path.exists():
        write_json(adjustments_path, {"schema_version": 1, "adjustments": []})
    write_json(state / "topix_rebalance_2026.json", {
        "schema_version": 1, "published_at": rebalance.published_at.isoformat(),
        "effective_date": rebalance.effective_date.isoformat(), "source_url": rebalance.source_url,
        "sha256": rebalance.sha256,
        "new": [{"security_code": c, "market_segment": s, "company_name": names.get(c, "")} for c, s in rebalance.new],
        "transition": [{"security_code": c, "market_segment": s, "company_name": names.get(c, "")}
                       for c, s in rebalance.transition],
        "constituents": [{"security_code": c, "market_segment": s} for c, s in rebalance.constituents],
    })
    LOG.info("wrote %s (%d bytes)", membership_path, len(dumps(membership)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
