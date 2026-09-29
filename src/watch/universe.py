"""JPX400 ユニバース（membership）の永続化・差分適用・期間判定。

membership の形:
{"schema_version": 1, "last_checked_at": None, "last_source_sha256": None, "last_as_of": None,
 "members": [{"security_code", "company_name", "effective_from", "effective_to"(None=現役),
              "source_url", "source_published_at", "seeded"}]}
日付は ISO 文字列。effective_from/to は両端を含む（from <= on <= to）。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from src.watch.sources.jpx400 import CODE_RE, ConstituentsSnapshot, normalize_security_code

DEFAULT_SEED_DATE = date(2026, 8, 31)
REBALANCE_REMOVED_TO = date(2026, 8, 28)
PRIOR_PERIOD_START = date(2025, 8, 29)  # 2025 年定期入替の適用日（除外銘柄の在籍開始の推定値）
SCHEMA_VERSION = 1


def _empty_membership() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_checked_at": None,
        "last_source_sha256": None,
        "last_as_of": None,
        "members": [],
    }


def load_membership(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return _empty_membership()
    data = json.loads(path.read_text(encoding="utf-8"))
    base = _empty_membership()
    base.update(data)
    base.setdefault("members", [])
    return base


def _sorted_members(members: list[dict]) -> list[dict]:
    return sorted(members, key=lambda m: (m["security_code"], m["effective_from"] or ""))


def dumps_membership(data: dict) -> str:
    out = dict(data)
    out["members"] = _sorted_members(list(data.get("members", [])))
    return json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def save_membership(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dumps_membership(data).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_previous_business_day(day: date) -> date:
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def _rejected(membership: dict, error: str, count: int) -> tuple[dict, dict]:
    return copy.deepcopy(membership), {
        "status": "rejected",
        "added": [],
        "removed": [],
        "member_count": count,
        "error": error,
    }


def _validate_snapshot(snapshot: ConstituentsSnapshot, min_members: int) -> str | None:
    rows = snapshot.rows
    if len(rows) < min_members:
        return f"構成銘柄数 {len(rows)} 件が下限 {min_members} 件未満"
    codes = [code for code, _ in rows]
    bad = [c for c in codes if not isinstance(c, str) or not CODE_RE.match(c)]
    if bad:
        return f"証券コード形式不正: {bad[:5]}"
    if len(set(codes)) != len(codes):
        return "証券コードが重複しています"
    return None


def apply_snapshot(
    membership: dict,
    snapshot: ConstituentsSnapshot,
    *,
    min_members: int = 350,
    seed_date: date = DEFAULT_SEED_DATE,
    previous_business_day: Callable[[date], date] | None = None,
) -> tuple[dict, dict]:
    """構成銘柄スナップショットを membership に適用する。入力 membership は変更しない。"""
    prev_bd = previous_business_day or _default_previous_business_day
    error = _validate_snapshot(snapshot, min_members)
    if error:
        return _rejected(membership, error, len(snapshot.rows))

    last_as_of = membership.get("last_as_of")
    if last_as_of and snapshot.as_of.isoformat() < last_as_of:
        return _rejected(
            membership,
            f"スナップショット基準日 {snapshot.as_of} が適用済み {last_as_of} より古い",
            len(snapshot.rows),
        )

    result = copy.deepcopy(membership)
    result.setdefault("members", [])
    names = dict(snapshot.rows)
    current = set(names)
    active = {m["security_code"]: m for m in result["members"] if m.get("effective_to") is None}
    published = snapshot.published_at

    added: list[str] = []
    removed: list[str] = []

    if not result["members"]:
        for code in sorted(current):
            result["members"].append(
                {
                    "security_code": code,
                    "company_name": names[code],
                    "effective_from": seed_date.isoformat(),
                    "effective_to": None,
                    "source_url": snapshot.source_url,
                    "source_published_at": published,
                    "seeded": True,
                }
            )
            added.append(code)
    else:
        to_date = prev_bd(snapshot.as_of)
        as_of_iso = snapshot.as_of.isoformat()
        adjustments = result.get("adjustments") or []
        # 公表日が調整の適用日より前の PDF は、その変更をまだ反映していない
        pending_adds = {a["security_code"] for a in adjustments
                        if a["action"] == "add" and a["effective_date"] > as_of_iso}
        pending_removes = {a["security_code"] for a in adjustments
                           if a["action"] == "remove" and a["effective_date"] > as_of_iso}
        latest_ended = set()
        for code in pending_removes - set(active):
            recs = [m for m in result["members"] if m["security_code"] == code]
            if recs and max(recs, key=lambda m: m["effective_from"]).get("effective_to") is not None:
                latest_ended.add(code)
        for code in sorted(set(active) - current - pending_adds):
            record = active[code]
            if record["effective_from"] > to_date.isoformat():
                result["members"].remove(record)  # 在籍日数ゼロ（追加当日に除外）
            else:
                record["effective_to"] = to_date.isoformat()
            removed.append(code)
        for code in sorted(current - set(active) - latest_ended):
            result["members"].append(
                {
                    "security_code": code,
                    "company_name": names[code],
                    "effective_from": snapshot.as_of.isoformat(),
                    "effective_to": None,
                    "source_url": snapshot.source_url,
                    "source_published_at": published,
                    "seeded": False,
                }
            )
            added.append(code)
        for code, record in active.items():
            if code in current and names[code] != record.get("company_name"):
                record["company_name"] = names[code]

    result["members"] = _sorted_members(result["members"])
    result["last_source_sha256"] = snapshot.sha256
    result["last_as_of"] = snapshot.as_of.isoformat()
    result["schema_version"] = SCHEMA_VERSION

    status = "ok" if (added or removed) else "unchanged"
    return result, {
        "status": status,
        "added": added,
        "removed": removed,
        "member_count": len(current),
        "error": None,
    }


def seed_with_rebalance(
    snapshot: ConstituentsSnapshot,
    removed: list[tuple[str, str]],
    added: list[str] | None = None,
    *,
    seed_date: date = DEFAULT_SEED_DATE,
    removed_to: date = REBALANCE_REMOVED_TO,
    removed_from: date = PRIOR_PERIOD_START,
    min_members: int = 350,
    source_url: str | None = None,
) -> tuple[dict, dict]:
    """初回シード。現行リストを seed_date から、定期入替の除外銘柄を removed_to までの在籍として登録する。

    除外銘柄の在籍開始 removed_from は前回定期入替の適用日による推定値（seeded=true が印）。
    added は定期入替での追加銘柄コード（情報用。現行リストに無いものがあれば report["warnings"] に載せる）。
    """
    membership, report = apply_snapshot(
        _empty_membership(), snapshot, min_members=min_members, seed_date=seed_date
    )
    if report["status"] == "rejected":
        return membership, report
    current = {m["security_code"] for m in membership["members"]}
    warnings: list[str] = []
    registered = 0
    for raw_code, name in removed:
        code = normalize_security_code(raw_code)
        if not code:
            warnings.append(f"除外銘柄コード不正: {raw_code}")
            continue
        if code in current:
            warnings.append(f"除外銘柄 {code} が現行リストに含まれるため登録しません")
            continue
        registered += 1
        membership["members"].append(
            {
                "security_code": code,
                "company_name": name,
                "effective_from": removed_from.isoformat(),
                "effective_to": removed_to.isoformat(),
                "source_url": source_url or snapshot.source_url,
                "source_published_at": snapshot.published_at,
                "seeded": True,
            }
        )
    for raw_code in added or []:
        code = normalize_security_code(raw_code)
        if code and code not in current:
            warnings.append(f"追加銘柄 {code} が現行リストにありません")
    membership["members"] = _sorted_members(membership["members"])
    report["warnings"] = warnings
    report["removed_registered"] = registered
    return membership, report


ADJUSTMENT_FIELDS = ("security_code", "company_name", "action", "effective_date", "announced_on", "source_url")


def load_adjustments(path: Path) -> list[dict]:
    """人手確認済みの調整ファイルを読む。無ければ []。不正なら ValueError。"""
    path = Path(path)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("jpx400_adjustments: schema_version が 1 ではありません")
    out: list[dict] = []
    for item in raw.get("adjustments", []):
        code = normalize_security_code(item.get("security_code"))
        if not code:
            raise ValueError(f"調整の証券コードが不正: {item.get('security_code')!r}")
        if item.get("action") not in ("add", "remove"):
            raise ValueError(f"調整 {code}: action が不正")
        try:
            eff = date.fromisoformat(item["effective_date"]).isoformat()
            ann = date.fromisoformat(item["announced_on"]).isoformat() if item.get("announced_on") else None
        except (KeyError, ValueError) as exc:
            raise ValueError(f"調整 {code}: 日付が不正") from exc
        out.append({
            "security_code": code,
            "company_name": item.get("company_name") or "",
            "action": item["action"],
            "effective_date": eff,
            "announced_on": ann,
            "source_url": item.get("source_url"),
        })
    return sorted(out, key=lambda a: (a["effective_date"], a["security_code"], a["action"]))


def apply_adjustments(
    membership: dict,
    adjustments: list[dict],
    *,
    previous_business_day: Callable[[date], date] | None = None,
) -> tuple[dict, dict]:
    """PDF 由来の membership の上に、公表済みの随時変更を決定的に重ねる（冪等）。

    調整は membership["adjustments"] に保存され、以後の apply_snapshot が参照する
    （PDF 更新で同じ変更が確認されても、日付を PDF 公表日へずらさない・反転しない）。
    """
    prev_bd = previous_business_day or _default_previous_business_day
    result = copy.deepcopy(membership)
    result.setdefault("members", [])
    members = result["members"]
    applied: list[str] = []
    warnings: list[str] = []
    ordered = sorted(adjustments, key=lambda a: (a["effective_date"], a["security_code"], a["action"]))
    for adj in ordered:
        code, eff = adj["security_code"], adj["effective_date"]
        recs = sorted((m for m in members if m["security_code"] == code), key=lambda m: m["effective_from"])
        if adj["action"] == "add":
            active = [m for m in recs if m.get("effective_to") is None]
            if active:
                rec = active[-1]
                if not rec.get("seeded") and rec["effective_from"] != eff:
                    rec["effective_from"] = eff
                    applied.append(f"add {code} (effective_from 補正)")
                continue
            members.append({
                "security_code": code,
                "company_name": adj["company_name"],
                "effective_from": eff,
                "effective_to": None,
                "source_url": adj["source_url"],
                "source_published_at": adj["announced_on"],
                "seeded": False,
                "adjusted": True,
            })
            applied.append(f"add {code}")
        else:
            to_date = prev_bd(date.fromisoformat(eff)).isoformat()
            target = [m for m in recs if m["effective_from"] <= to_date]
            if not target:
                warnings.append(f"remove {code}: 該当する在籍レコードがありません")
                continue
            rec = target[-1]
            if rec.get("effective_to") is None or rec["effective_to"] > to_date:
                rec["effective_to"] = to_date
                applied.append(f"remove {code}")
    result["members"] = _sorted_members(members)
    result["adjustments"] = ordered
    changed = dumps_membership(result) != dumps_membership(membership)
    return result, {
        "status": "ok" if changed else "unchanged",
        "applied": applied,
        "warnings": warnings,
        "member_count": sum(1 for m in result["members"] if m.get("effective_to") is None),
        "error": None,
    }


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class Universe:
    def __init__(self, membership: dict):
        self._names: dict[str, str] = {}
        self._periods: dict[str, list[tuple[str, str | None]]] = {}
        for member in membership.get("members", []):
            code = member["security_code"]
            self._periods.setdefault(code, []).append(
                (member["effective_from"], member.get("effective_to"))
            )
            if member.get("company_name") and (
                code not in self._names or member.get("effective_to") is None
            ):
                self._names[code] = member["company_name"]

    def is_member(self, code: str, on: date) -> bool:
        key = normalize_security_code(code) or str(code)
        day = _as_date(on).isoformat()
        for start, end in self._periods.get(key, ()):
            if start <= day and (end is None or day <= end):
                return True
        return False

    def members_on(self, on: date) -> set[str]:
        return {code for code in self._periods if self.is_member(code, on)}

    def all_codes(self) -> set[str]:
        return set(self._periods)

    def company_name(self, code: str) -> str | None:
        return self._names.get(normalize_security_code(code) or str(code))
