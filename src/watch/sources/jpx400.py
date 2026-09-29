"""JPX日経インデックス400 構成銘柄一覧（JPX公式PDF）の取得・解析。

公式ソース: https://www.jpx.co.jp/markets/indices/line-up/files/mei2_1_jpx400.pdf
（Excel 由来の PDF。2 段組みで「コード 市場区分 銘柄名」が 1 行に 2 組並ぶ。
先頭に「YYYY年M月D日公表」「構成銘柄数：N銘柄」がある。）
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

CONSTITUENTS_URL = "https://www.jpx.co.jp/markets/indices/line-up/files/mei2_1_jpx400.pdf"
USER_AGENT = "Mozilla/5.0 (compatible; stock-radar-watch/1.0; +https://github.com/highwing-republic/stock)"

CODE_RE = re.compile(r"^\d{3}[0-9A-Z]$")
_ROW_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d{3}[0-9A-Z])\s+([PSG])\s+(.+?)(?=\s+\d{3}[0-9A-Z]\s+[PSG]\s+|\s*$)"
)
_PUBLISHED_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*公表")
_COUNT_RE = re.compile(r"構成銘柄数\s*[：:]\s*(\d+)\s*銘柄")


def normalize_security_code(raw: object) -> str | None:
    """証券コードを 4 文字（英数字可。例 285A）へ正規化する。不正なら None。

    全角→半角、大文字化、5 桁で末尾 0 のコードは先頭 4 文字。
    """
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw)).strip().upper()
    text = text.replace(" ", "")
    if len(text) == 5 and text.endswith("0"):
        text = text[:4]
    return text if CODE_RE.match(text) else None


@dataclass(frozen=True)
class ConstituentsSnapshot:
    as_of: date
    source_url: str
    published_at: str | None
    rows: tuple[tuple[str, str], ...]  # (security_code, company_name)
    sha256: str


def extract_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_constituents_text(text: str, *, source_url: str, sha256: str = "") -> ConstituentsSnapshot:
    published = _PUBLISHED_RE.search(text)
    if not published:
        raise ValueError("公表日（YYYY年M月D日公表）が見つかりません")
    as_of = date(int(published.group(1)), int(published.group(2)), int(published.group(3)))

    rows: dict[str, str] = {}
    for line in text.splitlines():
        for match in _ROW_RE.finditer(line):
            code = normalize_security_code(match.group(1))
            if not code:
                continue
            name = unicodedata.normalize("NFKC", match.group(3)).strip()
            rows.setdefault(code, name)
    if not rows:
        raise ValueError("構成銘柄の行が 1 件も解析できません")

    declared = _COUNT_RE.search(text)
    if declared and int(declared.group(1)) != len(rows):
        raise ValueError(
            f"構成銘柄数の不一致: 記載 {declared.group(1)} 件 / 解析 {len(rows)} 件"
        )
    return ConstituentsSnapshot(
        as_of=as_of,
        source_url=source_url,
        published_at=as_of.isoformat(),
        rows=tuple(sorted(rows.items())),
        sha256=sha256,
    )


def parse_constituents(content: bytes, *, source_url: str) -> ConstituentsSnapshot:
    """JPX の構成銘柄一覧 PDF（バイト列）を解析する。"""
    digest = hashlib.sha256(content).hexdigest()
    return parse_constituents_text(extract_pdf_text(content), source_url=source_url, sha256=digest)


def fetch_constituents(session=None, *, timeout: float = 30) -> ConstituentsSnapshot:
    import requests

    http = session or requests.Session()
    response = http.get(CONSTITUENTS_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return parse_constituents(response.content, source_url=CONSTITUENTS_URL)
