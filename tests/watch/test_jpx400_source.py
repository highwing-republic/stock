import hashlib
from datetime import date
from pathlib import Path

import pytest

from src.watch.sources import jpx400
from src.watch.sources.jpx400 import (fetch_constituents, normalize_security_code,
                                      parse_constituents, parse_constituents_text)

FIXTURE = Path(__file__).parent / "fixtures" / "jpx400_constituents_20260807.txt"


def fixture_text():
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_real_layout_fixture():
    snap = parse_constituents_text(fixture_text(), source_url="u")
    assert snap.as_of == date(2026, 8, 7)
    assert snap.published_at == "2026-08-07"
    assert len(snap.rows) == 400
    names = dict(snap.rows)
    assert names["1332"] == "ニッスイ"
    # alphanumeric codes
    assert names["417A"] == "ブルーゾーンホールディングス"
    assert "547A" in names
    # full-width names are NFKC-normalised; both halves of the two-column layout are read
    assert names["3563"] == "FOOD & LIFE COMPANIES"
    assert names["9508"] == "九州電力"
    assert [c for c, _ in snap.rows] == sorted(names)


def test_count_mismatch_is_error():
    text = fixture_text().replace("構成銘柄数：400銘柄", "構成銘柄数：401銘柄")
    with pytest.raises(ValueError, match="不一致"):
        parse_constituents_text(text, source_url="u")


def test_missing_date_and_empty_are_errors():
    with pytest.raises(ValueError):
        parse_constituents_text("コード 市場区分 銘柄名\n1332 P ニッスイ", source_url="u")
    with pytest.raises(ValueError):
        parse_constituents_text("2026年8月7日公表\n何もない", source_url="u")


def test_normalize_security_code():
    assert normalize_security_code("１３３２") == "1332"
    assert normalize_security_code("13010") == "1301"
    assert normalize_security_code("285a0") == "285A"
    assert normalize_security_code(" 285A ") == "285A"
    assert normalize_security_code("12345") is None
    assert normalize_security_code("abc") is None
    assert normalize_security_code(None) is None


def test_parse_constituents_bytes_uses_pdf_text_and_hashes(monkeypatch):
    monkeypatch.setattr(jpx400, "extract_pdf_text", lambda content: content.decode("utf-8"))
    raw = fixture_text().encode("utf-8")
    snap = parse_constituents(raw, source_url="https://x/y.pdf")
    assert len(snap.rows) == 400 and snap.source_url == "https://x/y.pdf"
    assert snap.sha256 == hashlib.sha256(raw).hexdigest()


class FakeResp:
    def __init__(self, content, status=200):
        self.content, self.status = content, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, timeout, headers))
        return self.routes[url]


def test_fetch_constituents_with_fake_session(monkeypatch):
    monkeypatch.setattr(jpx400, "extract_pdf_text", lambda content: content.decode("utf-8"))
    session = FakeSession({jpx400.CONSTITUENTS_URL: FakeResp(fixture_text().encode("utf-8"))})
    snap = fetch_constituents(session, timeout=5)
    assert len(snap.rows) == 400
    url, timeout, headers = session.calls[0]
    assert timeout == 5 and "User-Agent" in headers


def test_fetch_constituents_http_error():
    session = FakeSession({jpx400.CONSTITUENTS_URL: FakeResp(b"", status=503)})
    with pytest.raises(RuntimeError):
        fetch_constituents(session)
