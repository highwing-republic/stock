"""TOPIX 構成銘柄の取得・解析（JPX 公式）。

2 つのソースを扱う。

1. 初回定期入替の選定結果 PDF（2026-10-07 公表、2026-10-30 実施）
   https://www.jpx.co.jp/markets/indices/topix/tvdivq00000030ne-att/topix_j.pdf
   （１）新規追加銘柄 35 ／（２）移行措置銘柄 683 ／（３）構成銘柄 1,669 の 3 節。
   各行は「No. コード 銘柄名 市場区分」。`parse_selection_pdf` が 3 節を返し、
   `scripts/seed_topix.py` が membership（= 構成 − 移行措置 = 986）と transition を生成する。
   実行時にこの PDF は取りに行かない。

2. 構成銘柄別ウエイト一覧 CSV（毎月末更新、cp932）
   https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv
   列: 日付, 銘柄名, コード, 業種, TOPIXに占める個別銘柄のウエイト, ニューインデックス区分
   `fetch_constituents` が `ConstituentsSnapshot`（as_of = 日付）を返す。移行措置銘柄の除外は
   pipeline 側（universe.transition_file）で行う。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from .jpx400 import USER_AGENT, ConstituentsSnapshot, normalize_security_code

SELECTION_PDF_URL = "https://www.jpx.co.jp/markets/indices/topix/tvdivq00000030ne-att/topix_j.pdf"
WEIGHT_CSV_URL = "https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv"
MARKETS = ("プライム", "スタンダード", "グロース")

_PUBLISHED_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*公表")
_EFFECTIVE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*実施")
_SECTION_RE = re.compile(r"[（(]\s*([１２３123])\s*[）)]\s*(新規追加銘柄|移行措置銘柄|構成銘柄)")
_ROW_RE = re.compile(r"^\s*(\d+)\s+(\d{3}[0-9A-Z])\s+(.+?)\s{2,}(プライム|スタンダード|グロース)\s*$")
_SECTION_KEYS = {"新規追加銘柄": "added", "移行措置銘柄": "transition", "構成銘柄": "constituents"}


@dataclass(frozen=True)
class SelectionResult:
    published_at: date
    effective_at: date | None
    source_url: str
    sha256: str
    added: tuple[tuple[str, str, str], ...]         # (code, name, market)
    transition: tuple[tuple[str, str, str], ...]
    constituents: tuple[tuple[str, str, str], ...]

    def continuing(self) -> tuple[tuple[str, str, str], ...]:
        """構成銘柄のうち新規追加でも移行措置でもないもの。"""
        skip = {c for c, _, _ in self.added} | {c for c, _, _ in self.transition}
        return tuple(r for r in self.constituents if r[0] not in skip)

    def members(self) -> tuple[tuple[str, str, str], ...]:
        """新 TOPIX のユニバース = 構成銘柄 − 移行措置銘柄。"""
        skip = {c for c, _, _ in self.transition}
        return tuple(r for r in self.constituents if r[0] not in skip)


def extract_pdf_text(content: bytes) -> str:
    """pypdf の layout モードで抽出する（既定モードでは列が分解され行が崩れる）。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text(extraction_mode="layout") or "") for page in reader.pages)


def _name(raw: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()


def parse_selection_text(text: str, *, source_url: str = SELECTION_PDF_URL, sha256: str = "") -> SelectionResult:
    published = _PUBLISHED_RE.search(text)
    if not published:
        raise ValueError("公表日（YYYY年M月D日公表）が見つかりません")
    published_at = date(*map(int, published.groups()))
    effective = _EFFECTIVE_RE.search(text)
    effective_at = date(*map(int, effective.groups())) if effective else None

    sections: dict[str, list[tuple[str, str, str]]] = {"added": [], "transition": [], "constituents": []}
    expected: dict[str, int] = {}
    current: str | None = None
    for line in text.splitlines():
        sec = _SECTION_RE.search(line)
        if sec:
            current = _SECTION_KEYS[sec.group(2)]
            expected[current] = 0
            continue
        if current is None:
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        no, raw_code, raw_name, market = m.groups()
        code = normalize_security_code(raw_code)
        if not code:
            raise ValueError(f"証券コード不正: {raw_code!r}（{current}）")
        expected[current] += 1
        if int(no) != expected[current]:
            raise ValueError(f"{current}: No. が連番でありません（期待 {expected[current]}、実際 {no}）")
        sections[current].append((code, _name(raw_name), market))

    for key, rows in sections.items():
        if not rows:
            raise ValueError(f"{key}: 行が 1 件も解析できません")
        codes = [c for c, _, _ in rows]
        if len(set(codes)) != len(codes):
            raise ValueError(f"{key}: 証券コードが重複しています")
    const = {c for c, _, _ in sections["constituents"]}
    for key in ("added", "transition"):
        missing = [c for c, _, _ in sections[key] if c not in const]
        if missing:
            raise ValueError(f"{key} のコードが構成銘柄に含まれていません: {missing[:5]}")
    overlap = {c for c, _, _ in sections["added"]} & {c for c, _, _ in sections["transition"]}
    if overlap:
        raise ValueError(f"新規追加と移行措置が重複しています: {sorted(overlap)[:5]}")

    return SelectionResult(
        published_at=published_at, effective_at=effective_at, source_url=source_url, sha256=sha256,
        added=tuple(sections["added"]), transition=tuple(sections["transition"]),
        constituents=tuple(sections["constituents"]),
    )


def parse_selection_pdf(content: bytes, *, source_url: str = SELECTION_PDF_URL) -> SelectionResult:
    digest = hashlib.sha256(content).hexdigest()
    return parse_selection_text(extract_pdf_text(content), source_url=source_url, sha256=digest)


# --------------------------------------------------------------------------- 月次ウエイト CSV
CSV_COLUMNS = ("日付", "銘柄名", "コード")


def parse_weight_csv(content: bytes, *, source_url: str = WEIGHT_CSV_URL) -> ConstituentsSnapshot:
    """構成銘柄別ウエイト一覧 CSV（cp932）を解析する。ウエイト列は使わない。"""
    digest = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("cp932")
    except UnicodeDecodeError:
        text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fields = [unicodedata.normalize("NFKC", f).strip() for f in (reader.fieldnames or [])]
    missing = [c for c in CSV_COLUMNS if c not in fields]
    if missing:
        raise ValueError(f"CSV の列が不足しています: {missing}（実際 {fields}）")
    col = {unicodedata.normalize("NFKC", f).strip(): f for f in (reader.fieldnames or [])}

    as_of: date | None = None
    rows: dict[str, str] = {}
    for rec in reader:
        raw_date = (rec.get(col["日付"]) or "").strip()
        raw_code = rec.get(col["コード"])
        if not raw_date or not raw_code:
            continue
        day = _parse_yyyymmdd(raw_date)
        if as_of is None:
            as_of = day
        elif day != as_of:
            raise ValueError(f"CSV に複数の日付が混在しています: {as_of} と {day}")
        code = normalize_security_code(raw_code)
        if not code:
            raise ValueError(f"証券コード不正: {raw_code!r}")
        rows.setdefault(code, _name(rec.get(col["銘柄名"]) or ""))
    if as_of is None or not rows:
        raise ValueError("CSV から構成銘柄を 1 件も読めません")
    return ConstituentsSnapshot(as_of=as_of, source_url=source_url, published_at=as_of.isoformat(),
                                rows=tuple(sorted(rows.items())), sha256=digest)


def _parse_yyyymmdd(raw: str) -> date:
    digits = re.sub(r"\D", "", unicodedata.normalize("NFKC", raw))
    if len(digits) != 8:
        raise ValueError(f"日付形式不正: {raw!r}")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def fetch_constituents(session=None, *, timeout: float = 30) -> ConstituentsSnapshot:
    import requests

    http = session or requests.Session()
    response = http.get(WEIGHT_CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return parse_weight_csv(response.content, source_url=WEIGHT_CSV_URL)


def fetch_selection(session=None, *, timeout: float = 60) -> SelectionResult:
    import requests

    http = session or requests.Session()
    response = http.get(SELECTION_PDF_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return parse_selection_pdf(response.content, source_url=SELECTION_PDF_URL)
