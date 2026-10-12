from datetime import date

import pytest

from src.watch.sources import topix
from src.watch.sources.topix import (RebalanceLists, exclude_codes, fetch_constituents, parse_rebalance_pdf_text,
                                     parse_weight_csv)
from src.watch.universe import Universe, apply_snapshot, seed_topix

HEADER = "日付,銘柄名,コード,業種,TOPIXに占める個別銘柄のウエイト,ニューインデックス区分\n"
NOTES = ("（注1）ニューインデックス区分欄には、「TOPIX Core30」等を記載します。,,,,,\n"
         "・本資料は、予告なく変更される場合があります。,,,,,\n")


def csv_bytes(rows, as_of="20260831", notes=NOTES, encoding="cp932"):
    body = "".join(f"{as_of},{name},{code},{sector},0.0100%,TOPIX Mid400\n" for code, name, sector in rows)
    return (HEADER + body + notes).encode(encoding)


ROWS = [("1301", "極洋", "水産・農林業"), ("1332", "ニッスイ", "水産・農林業"), ("285A", "キオクシアホールディングス", "電気機器"),
        ("７２０３", "トヨタ自動車", "輸送用機器")]


def test_parse_weight_csv_cp932_with_notes():
    snap = parse_weight_csv(csv_bytes(ROWS), source_url="u", sha256="h")
    assert snap.as_of == date(2026, 8, 31) and snap.published_at == "2026-08-31"
    assert dict(snap.rows) == {"1301": "極洋", "1332": "ニッスイ", "285A": "キオクシアホールディングス", "7203": "トヨタ自動車"}
    assert [c for c, _ in snap.rows] == sorted(dict(snap.rows))
    assert snap.sha256 == "h" and snap.source_url == "u"


def test_parse_weight_csv_errors():
    with pytest.raises(ValueError, match="明細行"):
        parse_weight_csv((HEADER + NOTES).encode("cp932"))
    with pytest.raises(ValueError, match="重複"):
        parse_weight_csv(csv_bytes(ROWS + [("1301", "極洋2", "x")]))
    with pytest.raises(ValueError, match="複数の日付"):
        parse_weight_csv(csv_bytes(ROWS[:2]) + csv_bytes(ROWS[2:], as_of="20260930")[len(HEADER.encode("cp932")):])
    with pytest.raises(ValueError, match="形式不正"):
        parse_weight_csv(csv_bytes([("12", "x", "y")]))
    with pytest.raises(ValueError, match="列が想定"):
        parse_weight_csv("日付,なまえ,ばんごう\n20260831,a,1301\n".encode("cp932"))


def test_parse_weight_csv_utf8_bom():
    snap = parse_weight_csv(b"\xef\xbb\xbf" + csv_bytes(ROWS, encoding="utf-8"))
    assert len(snap.rows) == 4


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


def test_fetch_constituents_uses_user_agent_and_hash():
    raw = csv_bytes(ROWS)
    session = FakeSession({topix.WEIGHT_CSV_URL: FakeResp(raw)})
    snap = fetch_constituents(session, timeout=7)
    assert len(snap.rows) == 4 and snap.source_url == topix.WEIGHT_CSV_URL and len(snap.sha256) == 64
    url, timeout, headers = session.calls[0]
    assert timeout == 7 and "User-Agent" in headers
    with pytest.raises(RuntimeError):
        fetch_constituents(FakeSession({topix.WEIGHT_CSV_URL: FakeResp(b"", 503)}))


# ---------------------------------------------------------------- 定期入替 PDF
PDF_TEXT = """2026年10月7日公表
（１）新規追加銘柄
No. コード 市場区分
1 1723 スタンダード
2 141A グロース
（２）移行措置銘柄
No. コード 市場区分
1 1301 プライム
2 1376 スタンダード
TOPIX初回定期入替における選定結果(2026年10月30日実施）及び構成銘柄一覧
ｓａｎｔｅｃ　Ｈｏｌｄｉｎｇｓ
精工技研
3 9508 プライム
（３）構成銘柄（2026年10月30日時点）
No. コード 市場区分
1 1301 プライム
2 1332 プライム
3 1376 スタンダード
4 1723 スタンダード
5 7203 プライム淺沼組
6 9508 プライム
7 141A グロース
（注）　銘柄コード順に掲載。本資料は公表日の情報に基づくもので、今後変動する可能性があります。
クラサスケミカル
8 9999 プライム
"""
EXPECTED = {"new": 2, "transition": 3, "constituents": 7}


def test_parse_rebalance_pdf_text_sections_and_segment_suffix():
    r = parse_rebalance_pdf_text(PDF_TEXT, expected=EXPECTED, sha256="s")
    assert r.published_at == date(2026, 10, 7) and r.effective_date == date(2026, 10, 30)
    assert r.codes("new") == {"1723", "141A"}
    assert r.codes("transition") == {"1301", "1376", "9508"}          # 節の途中に混ざる別表の銘柄名は無視
    assert r.codes("constituents") == {"1301", "1332", "1376", "1723", "7203", "9508", "141A"}
    assert dict(r.constituents)["7203"] == "プライム"                  # 「プライム淺沼組」のような連結行
    assert "9999" not in r.codes("constituents")                        # （注）以降は読まない
    assert r.codes("constituents") - r.codes("transition") == {"1332", "1723", "7203", "141A"}


def test_parse_rebalance_pdf_text_count_mismatch_and_subset_errors():
    with pytest.raises(ValueError, match="想定と一致しません"):
        parse_rebalance_pdf_text(PDF_TEXT, expected=dict(EXPECTED, new=3))
    with pytest.raises(ValueError, match="想定と一致しません"):
        parse_rebalance_pdf_text(PDF_TEXT)                              # 既定は 35/683/1669 の実数
    broken = PDF_TEXT.replace("4 1723 スタンダード\n", "")
    with pytest.raises(ValueError, match="含まれていません"):
        parse_rebalance_pdf_text(broken, expected=dict(EXPECTED, constituents=6))


def test_parse_rebalance_pdf_bytes(monkeypatch):
    monkeypatch.setattr(topix, "extract_pdf_text", lambda content: content.decode("utf-8"))
    r = topix.parse_rebalance_pdf(PDF_TEXT.encode("utf-8"), expected=EXPECTED)
    assert len(r.sha256) == 64 and r.source_url == topix.REBALANCE_PDF_URL_2026


def test_exclude_codes():
    snap = parse_weight_csv(csv_bytes(ROWS))
    out = exclude_codes(snap, {"1301", "285A"})
    assert [c for c, _ in out.rows] == ["1332", "7203"] and out.as_of == snap.as_of
    assert exclude_codes(snap, set()) is snap


# ---------------------------------------------------------------- seed_topix と継続更新
def rebalance():
    return parse_rebalance_pdf_text(PDF_TEXT, expected=EXPECTED, sha256="pdfsha")


NAMES = {"1301": "極洋", "1332": "ニッスイ", "1376": "カネコ種苗", "1723": "日本電技", "7203": "トヨタ自動車",
         "9508": "九州電力"}


def test_seed_topix_excludes_transition_by_default():
    m, rep = seed_topix(rebalance(), NAMES)
    assert rep["status"] == "ok" and rep["member_count"] == 4
    assert rep["continuing"] == 2 and rep["new"] == 2 and rep["excluded"] == 3 and rep["unnamed"] == ["141A"]
    assert m["excluded_codes"] == ["1301", "1376", "9508"] and m["last_as_of"] == "2026-10-30"
    assert m["rebalance"]["constituents"] == 7 and m["last_source_sha256"] == "pdfsha"
    by = {x["security_code"]: x for x in m["members"]}
    assert set(by) == {"1332", "7203", "1723", "141A"}
    assert by["1332"]["effective_from"] == "2026-04-01" and by["1332"]["seeded"] is True
    assert by["1723"]["effective_from"] == "2026-10-30" and by["1723"]["seeded"] is False
    assert by["141A"]["company_name"] == "141A"                           # 名前が無ければコードを仮名に
    u = Universe(m)
    assert u.is_member("1332", date(2026, 10, 1)) and not u.is_member("1332", date(2026, 3, 31))
    assert not u.is_member("1723", date(2026, 10, 29)) and u.is_member("1723", date(2026, 10, 30))
    assert not u.is_member("1301", date(2026, 11, 2))                     # 移行措置は対象外
    assert len(u.members_on(date(2026, 10, 30))) == 4 and len(u.members_on(date(2026, 10, 15))) == 2


def test_seed_topix_include_transition():
    m, rep = seed_topix(rebalance(), NAMES, include_transition=True)
    assert rep["member_count"] == 7 and rep["excluded"] == 0 and m["excluded_codes"] == []
    assert Universe(m).is_member("1301", date(2026, 6, 1))


def test_weight_csv_snapshot_after_seed_is_rejected_until_it_catches_up():
    """8/31 時点の CSV（移行措置銘柄入り・新規銘柄なし）は last_as_of=10/30 より古いので適用されない。"""
    m, _ = seed_topix(rebalance(), NAMES)
    old = parse_weight_csv(csv_bytes(ROWS[:2] + [("1376", "カネコ種苗", "x"), ("9508", "九州電力", "x")]))
    m2, rep = apply_snapshot(m, exclude_codes(old, set(m["excluded_codes"])), min_members=1)
    assert rep["status"] == "ignored" and "古い" in rep["error"] and m2["members"] == m["members"]


def test_weight_csv_snapshot_after_effective_date_applies_only_non_excluded():
    """11 月末更新の CSV（10/30 時点以降）: 移行措置銘柄は除外して適用、新規上場は追加、消えた銘柄は除外日を付ける。"""
    m, _ = seed_topix(rebalance(), NAMES)
    rows = [("1301", "極洋", "x"), ("1332", "ニッスイ", "x"), ("1723", "日本電技", "x"), ("141A", "新規", "x"),
            ("5555", "上場したて", "x")]                                    # 7203 が消え、5555 が増え、1301(移行) は残る
    new = parse_weight_csv(csv_bytes(rows, as_of="20261130"))
    m2, rep = apply_snapshot(m, exclude_codes(new, set(m["excluded_codes"])), min_members=1)
    assert rep["status"] == "ok" and rep["added"] == ["5555"] and rep["removed"] == ["7203"]
    u = Universe(m2)
    assert not u.is_member("1301", date(2026, 12, 1))                     # 移行措置は入ってこない
    assert u.is_member("5555", date(2026, 11, 30)) and not u.is_member("7203", date(2026, 11, 30))
    assert u.is_member("7203", date(2026, 11, 27))                        # 前営業日まで在籍
    assert u.company_name("141A") == "新規"                               # 名前が CSV で埋まる
    assert m2["excluded_codes"] == ["1301", "1376", "9508"]               # 除外リストは引き継がれる


def test_rebalance_lists_codes_helper():
    r = RebalanceLists(date(2026, 10, 7), date(2026, 10, 30), (("1", "P"),), (("2", "P"),), (("1", "P"), ("2", "P")),
                       "h", "u")
    assert r.codes("new") == {"1"} and r.codes("constituents") == {"1", "2"}
