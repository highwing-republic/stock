"""TOPIX 構成銘柄（JPX 公式）の取得・解析。

継続更新: 「TOPIX 構成銘柄ウエイト一覧」CSV（cp932、翌月末更新＝約 1 か月遅れ）
  https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv
  ヘッダ: 日付,銘柄名,コード,業種,TOPIXに占める個別銘柄のウエイト,ニューインデックス区分
  末尾に注記行（コード列が空）が続く。

初回シード: 2026-10-07 公表「TOPIX初回定期入替における選定結果(2026年10月30日実施)及び構成銘柄一覧」PDF
  https://www.jpx.co.jp/news/6030/t13vrt00000262wd-att/topix_j.pdf
  （１）新規追加銘柄 / （２）移行措置銘柄 / （３）構成銘柄（2026年10月30日時点）の 3 節。
  明細行は「No. コード 市場区分」で銘柄名列は無い（別表の銘柄名が 2 段組みで混入するため無視する）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from .common import USER_AGENT, ConstituentsSnapshot, extract_pdf_text, normalize_security_code

WEIGHT_CSV_URL = "https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv"
REBALANCE_PDF_URL_2026 = "https://www.jpx.co.jp/news/6030/t13vrt00000262wd-att/topix_j.pdf"
REBALANCE_2026 = {"published_at": date(2026, 10, 7), "effective_date": date(2026, 10, 30),
                  "expected": {"new": 35, "transition": 683, "constituents": 1669}}

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_SEGMENTS = ("プライム", "スタンダード", "グロース")
_ROW_RE = re.compile(r"^\s*(\d+)\s+(\d{3}[0-9A-Za-z])\s+(プライム|スタンダード|グロース)")
_SECTION_RE = {
    "new": re.compile(r"^[（(]\s*[1１]\s*[）)]\s*新規追加銘柄"),
    "transition": re.compile(r"^[（(]\s*[2２]\s*[）)]\s*移行措置銘柄"),
    "constituents": re.compile(r"^[（(]\s*[3３]\s*[）)]\s*構成銘柄"),
}


# --------------------------------------------------------------------------- ウエイト一覧 CSV
def _decode(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")
    try:
        return content.decode("cp932")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


def parse_weight_csv(content: bytes, *, source_url: str = WEIGHT_CSV_URL, sha256: str = "") -> ConstituentsSnapshot:
    """ウエイト一覧 CSV を (コード, 銘柄名) のスナップショットにする。注記行は捨てる。"""
    reader = csv.reader(io.StringIO(_decode(content)))
    header = next(reader, None)
    if not header or len(header) < 3:
        raise ValueError("TOPIX ウエイト一覧のヘッダが読めません")
    cols = [unicodedata.normalize("NFKC", (c or "")).strip() for c in header]
    try:
        i_date = next(i for i, c in enumerate(cols) if c.startswith("日付"))
        i_name = next(i for i, c in enumerate(cols) if "銘柄名" in c)
        i_code = next(i for i, c in enumerate(cols) if c == "コード" or c.endswith("コード"))
    except StopIteration as exc:
        raise ValueError(f"TOPIX ウエイト一覧の列が想定と違います: {cols}") from exc
    rows: dict[str, str] = {}
    as_of: date | None = None
    for row in reader:
        if len(row) <= max(i_date, i_name, i_code):
            continue
        m = _DATE_RE.match(row[i_date].strip())
        if not m:
            continue  # 注記行など
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if as_of is None:
            as_of = day
        elif day != as_of:
            raise ValueError(f"TOPIX ウエイト一覧に複数の日付が混在: {as_of} / {day}")
        code = normalize_security_code(row[i_code])
        if not code:
            raise ValueError(f"証券コード形式不正: {row[i_code]!r}")
        if code in rows:
            raise ValueError(f"証券コードが重複: {code}")
        rows[code] = unicodedata.normalize("NFKC", row[i_name]).strip()
    if as_of is None or not rows:
        raise ValueError("TOPIX ウエイト一覧に明細行がありません")
    return ConstituentsSnapshot(as_of=as_of, source_url=source_url, published_at=as_of.isoformat(),
                                rows=tuple(sorted(rows.items())), sha256=sha256)


def fetch_constituents(session=None, *, timeout: float = 30) -> ConstituentsSnapshot:
    import requests

    http = session or requests.Session()
    response = http.get(WEIGHT_CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    content = response.content
    return parse_weight_csv(content, source_url=WEIGHT_CSV_URL, sha256=hashlib.sha256(content).hexdigest())


# --------------------------------------------------------------------------- 定期入替 PDF（シード用）
@dataclass(frozen=True)
class RebalanceLists:
    published_at: date
    effective_date: date
    new: tuple[tuple[str, str], ...]           # (security_code, market_segment)
    transition: tuple[tuple[str, str], ...]
    constituents: tuple[tuple[str, str], ...]
    sha256: str
    source_url: str

    def codes(self, key: str) -> set[str]:
        return {c for c, _ in getattr(self, key)}


def parse_rebalance_pdf_text(text: str, *, expected: dict | None = None, published_at: date | None = None,
                             effective_date: date | None = None, sha256: str = "",
                             source_url: str = REBALANCE_PDF_URL_2026) -> RebalanceLists:
    """定期入替 PDF のテキストから 3 節の (コード, 市場区分) を取り出す。件数が想定と違えば ValueError。"""
    meta = REBALANCE_2026
    expected = meta["expected"] if expected is None else expected
    sections: dict[str, dict[str, str]] = {"new": {}, "transition": {}, "constituents": {}}
    current: str | None = None
    for raw in text.splitlines():
        line = unicodedata.normalize("NFKC", raw).strip()
        if not line:
            continue
        for key, pat in _SECTION_RE.items():
            if pat.match(line):
                current = key
                break
        else:
            if current is None or line.startswith("(注)"):
                if line.startswith("(注)"):
                    current = None
                continue
            m = _ROW_RE.match(line)
            if not m:
                continue
            code = normalize_security_code(m.group(2))
            if not code:
                continue
            sections[current].setdefault(code, m.group(3))
    errors = []
    for key, want in (expected or {}).items():
        got = len(sections[key])
        if got != want:
            errors.append(f"{key}: 解析 {got} 件 / 想定 {want} 件")
    if not sections["constituents"]:
        errors.append("constituents: 0 件")
    if errors:
        raise ValueError("定期入替 PDF の解析結果が想定と一致しません: " + "; ".join(errors))
    cons = set(sections["constituents"])
    if not set(sections["transition"]) <= cons or not set(sections["new"]) <= cons:
        raise ValueError("定期入替 PDF: 新規・移行措置銘柄が構成銘柄一覧に含まれていません")
    return RebalanceLists(
        published_at=published_at or meta["published_at"], effective_date=effective_date or meta["effective_date"],
        new=tuple(sorted(sections["new"].items())), transition=tuple(sorted(sections["transition"].items())),
        constituents=tuple(sorted(sections["constituents"].items())), sha256=sha256, source_url=source_url)


def parse_rebalance_pdf(content: bytes, **kw) -> RebalanceLists:
    return parse_rebalance_pdf_text(extract_pdf_text(content), sha256=hashlib.sha256(content).hexdigest(), **kw)


def fetch_rebalance_pdf(url: str = REBALANCE_PDF_URL_2026, session=None, *, timeout: float = 60) -> RebalanceLists:
    import requests

    http = session or requests.Session()
    response = http.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return parse_rebalance_pdf(response.content, source_url=url)


def exclude_codes(snapshot: ConstituentsSnapshot, excluded: set[str]) -> ConstituentsSnapshot:
    """移行措置銘柄などをスナップショットから除く（include_transition=false 用）。"""
    if not excluded:
        return snapshot
    rows = tuple(r for r in snapshot.rows if r[0] not in excluded)
    return ConstituentsSnapshot(as_of=snapshot.as_of, source_url=snapshot.source_url,
                                published_at=snapshot.published_at, rows=rows, sha256=snapshot.sha256)
