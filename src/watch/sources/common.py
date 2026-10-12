"""構成銘柄ソース共通: 証券コード正規化・スナップショット型・PDF テキスト抽出。"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

USER_AGENT = "Mozilla/5.0 (compatible; stock-radar-watch/1.0; +https://github.com/highwing-republic/stock)"
CODE_RE = re.compile(r"^\d{3}[0-9A-Z]$")


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
