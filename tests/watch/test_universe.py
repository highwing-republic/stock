import json
from datetime import date
from pathlib import Path

from src.watch.sources.jpx400 import ConstituentsSnapshot, parse_constituents_text
from src.watch.universe import (DEFAULT_SEED_DATE, Universe, apply_snapshot,
                                load_membership, save_membership, seed_with_rebalance)

FIXTURE = Path(__file__).parent / "fixtures" / "jpx400_constituents_20260807.txt"
MISSING = Path("/nonexistent/dir/membership.json")


def snap(codes, as_of, sha="h", names=None):
    rows = tuple(sorted((c, (names or {}).get(c, f"name{c}")) for c in codes))
    return ConstituentsSnapshot(as_of=as_of, source_url="u", published_at=as_of.isoformat(),
                                rows=rows, sha256=sha)


def codes(n, extra=()):
    return [f"{1000 + i}" for i in range(n)] + list(extra)


def seeded(n=360):
    m, rep = apply_snapshot(load_membership(MISSING), snap(codes(n), date(2026, 8, 7)))
    assert rep["status"] == "ok" and rep["member_count"] == n
    return m


def test_load_missing_returns_empty(tmp_path):
    m = load_membership(tmp_path / "none.json")
    assert m["schema_version"] == 1 and m["members"] == []


def test_seed_uses_seed_date_and_flag():
    m = seeded()
    assert {x["effective_from"] for x in m["members"]} == {"2026-08-31"}
    assert all(x["seeded"] and x["effective_to"] is None for x in m["members"])
    u = Universe(m)
    assert u.is_member("1000", date(2026, 8, 31))
    assert not u.is_member("1000", date(2026, 8, 30))  # before seed date: unknown = non-member
    assert len(u.members_on(date(2026, 9, 29))) == 360


def test_addition_and_irregular_removal_effective_dates():
    m = seeded()
    # 2026-10-02 (Fri) snapshot: 1000 removed, 9999 added
    m2, rep = apply_snapshot(m, snap(codes(360)[1:] + ["9999"], date(2026, 10, 2)))
    assert rep["status"] == "ok" and rep["added"] == ["9999"] and rep["removed"] == ["1000"]
    u = Universe(m2)
    assert u.is_member("1000", date(2026, 10, 1))  # previous weekday of as_of is the last day
    assert not u.is_member("1000", date(2026, 10, 2))
    assert not u.is_member("9999", date(2026, 10, 1))
    assert u.is_member("9999", date(2026, 10, 2))
    assert "1000" in u.all_codes() and "9999" in u.all_codes()


def test_default_previous_business_day_skips_weekend():
    m = seeded()
    m2, _ = apply_snapshot(m, snap(codes(360)[1:], date(2026, 10, 5)))  # Monday
    rec = next(x for x in m2["members"] if x["security_code"] == "1000")
    assert rec["effective_to"] == "2026-10-02"


def test_previous_business_day_callable_is_used():
    m = seeded()
    calls = []

    def pbd(d):
        calls.append(d)
        return date(2026, 9, 25)

    m2, _ = apply_snapshot(m, snap(codes(360)[1:], date(2026, 9, 29)), previous_business_day=pbd)
    assert calls == [date(2026, 9, 29)]
    rec = next(x for x in m2["members"] if x["security_code"] == "1000")
    assert rec["effective_to"] == "2026-09-25"


def test_readd_after_removal_creates_second_period():
    m = seeded()
    m, _ = apply_snapshot(m, snap(codes(360)[1:], date(2026, 10, 1)))
    m, rep = apply_snapshot(m, snap(codes(360), date(2026, 11, 2)))
    assert rep["added"] == ["1000"]
    periods = [x for x in m["members"] if x["security_code"] == "1000"]
    assert [(p["effective_from"], p["effective_to"]) for p in periods] == [
        ("2026-08-31", "2026-09-30"), ("2026-11-02", None)]
    u = Universe(m)
    assert u.is_member("1000", date(2026, 9, 30))
    assert not u.is_member("1000", date(2026, 10, 15))
    assert u.is_member("1000", date(2026, 11, 2))


def test_rejects_too_few_rows_and_keeps_membership():
    m = seeded()
    m2, rep = apply_snapshot(m, snap(codes(100), date(2026, 10, 1)))
    assert rep["status"] == "rejected" and "350" in rep["error"]
    assert m2 == m and m2 is not m
    assert rep["added"] == [] and rep["removed"] == []


def test_rejects_malformed_and_duplicate_codes():
    m = seeded()
    for bad in (["12"], ["ABCD"], ["１３３２"], ["12345"]):
        _, rep = apply_snapshot(m, snap(codes(360) + bad, date(2026, 10, 1)))
        assert rep["status"] == "rejected", bad
    dup = ConstituentsSnapshot(date(2026, 10, 1), "u", None,
                               tuple((c, "n") for c in codes(360)) + (("1000", "n"),), "h")
    assert apply_snapshot(m, dup)[1]["status"] == "rejected"


def test_rejects_older_snapshot():
    m = seeded()
    _, rep = apply_snapshot(m, snap(codes(360)[1:], date(2026, 7, 1), sha="old"))
    assert rep["status"] == "rejected" and "古い" in rep["error"]


def test_alphanumeric_codes_supported():
    m, rep = apply_snapshot(load_membership(MISSING), snap(codes(360, ["285A"]), date(2026, 8, 7)))
    assert rep["status"] == "ok"
    assert Universe(m).is_member("285A", date(2026, 9, 1))


def test_idempotent_apply_and_byte_identical_save(tmp_path):
    s = snap(codes(360), date(2026, 8, 7), sha="abc")
    m1, r1 = apply_snapshot(load_membership(tmp_path / "m.json"), s)
    save_membership(tmp_path / "m.json", m1)
    first = (tmp_path / "m.json").read_bytes()
    m2, r2 = apply_snapshot(load_membership(tmp_path / "m.json"), s)
    assert r1["status"] == "ok" and r2["status"] == "unchanged"
    assert r2["added"] == [] and r2["removed"] == [] and r2["member_count"] == 360
    save_membership(tmp_path / "m.json", m2)
    assert (tmp_path / "m.json").read_bytes() == first
    assert not list(tmp_path.glob("*.tmp"))


def test_save_is_deterministic_json(tmp_path):
    m = seeded()
    m["members"] = list(reversed(m["members"]))
    save_membership(tmp_path / "sub" / "m.json", m)
    text = (tmp_path / "sub" / "m.json").read_text(encoding="utf-8")
    data = json.loads(text)
    ordered = [(x["security_code"], x["effective_from"]) for x in data["members"]]
    assert ordered == sorted(ordered)
    assert text == json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def test_name_change_is_applied_without_status_change():
    m = seeded()
    s = snap(codes(360), date(2026, 8, 20), sha="new", names={"1000": "改名後"})
    m2, rep = apply_snapshot(m, s)
    assert rep["status"] == "unchanged"
    assert Universe(m2).company_name("1000") == "改名後"
    assert m2["last_source_sha256"] == "new"


def test_universe_lookup_helpers():
    u = Universe(seeded())
    assert u.company_name("1000") == "name1000"
    assert u.company_name("0000") is None
    assert not u.is_member("ZZZZ", date(2026, 9, 1))
    assert u.is_member("10000", date(2026, 9, 1))  # 5-digit notation accepted


def test_seed_with_rebalance_registers_removed_codes():
    s = parse_constituents_text(FIXTURE.read_text(encoding="utf-8"), source_url="u")
    removed = [("1419", "タマホーム"), ("2168", "パソナグループ"), ("bad", "x"), ("1332", "ニッスイ")]
    m, rep = seed_with_rebalance(s, removed, ["1803", "0000"])
    assert rep["status"] == "ok" and rep["removed_registered"] == 2
    assert len(rep["warnings"]) == 3  # bad code, removed-but-current, added-but-missing
    u = Universe(m)
    assert u.is_member("1419", date(2026, 8, 28)) and not u.is_member("1419", date(2026, 8, 31))
    assert not u.is_member("1419", date(2025, 1, 1))  # before the prior rebalance: unknown
    assert u.is_member("1803", DEFAULT_SEED_DATE) and not u.is_member("1803", date(2026, 8, 28))
    tama = next(x for x in m["members"] if x["security_code"] == "1419")
    assert tama["seeded"] is True and tama["effective_to"] == "2026-08-28"
    assert len(u.members_on(date(2026, 9, 1))) == 400
    m2, rep2 = apply_snapshot(m, s)
    assert rep2["status"] == "unchanged" and m2["members"] == m["members"]


# ---------------------------------------------------------------- adjustments

from src.watch.universe import apply_adjustments, load_adjustments  # noqa: E402

ADJ = [
    {"security_code": "9999", "company_name": "新規", "action": "add", "effective_date": "2026-09-29",
     "announced_on": "2026-09-09", "source_url": "https://example/a"},
    {"security_code": "1000", "company_name": "旧", "action": "remove", "effective_date": "2026-10-01",
     "announced_on": "2026-09-03", "source_url": "https://example/r"},
]


def test_load_adjustments_missing_and_validation(tmp_path):
    assert load_adjustments(tmp_path / "none.json") == []
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"schema_version": 1, "adjustments": [dict(ADJ[0], security_code="１２３Ａ")]}),
                 encoding="utf-8")
    assert load_adjustments(p)[0]["security_code"] == "123A"
    for bad in (dict(ADJ[0], action="x"), dict(ADJ[0], security_code="zz"), dict(ADJ[0], effective_date="bad")):
        p.write_text(json.dumps({"schema_version": 1, "adjustments": [bad]}), encoding="utf-8")
        try:
            load_adjustments(p)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_apply_adjustments_effective_dates_and_idempotence(tmp_path):
    m = seeded()
    m1, rep = apply_adjustments(m, ADJ)
    assert rep["status"] == "ok" and rep["member_count"] == 360
    u = Universe(m1)
    assert not u.is_member("9999", date(2026, 9, 28)) and u.is_member("9999", date(2026, 9, 29))
    assert u.is_member("1000", date(2026, 9, 30)) and not u.is_member("1000", date(2026, 10, 1))
    m2, rep2 = apply_adjustments(m1, ADJ)
    assert rep2["status"] == "unchanged"
    save_membership(tmp_path / "a.json", m1)
    save_membership(tmp_path / "b.json", m2)
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


def test_snapshot_before_effective_date_does_not_undo_adjustments():
    m, _ = apply_adjustments(seeded(), ADJ)
    same_pdf = snap(codes(360), date(2026, 8, 7), sha="h")  # lists 1000, lacks 9999
    m2, rep = apply_snapshot(m, same_pdf)
    assert rep["status"] == "unchanged" and rep["removed"] == [] and rep["added"] == []
    u = Universe(m2)
    assert u.is_member("9999", date(2026, 9, 29)) and not u.is_member("1000", date(2026, 10, 1))
    assert len([x for x in m2["members"] if x["security_code"] == "1000"]) == 1
    m3, _ = apply_adjustments(m2, ADJ)
    assert m3 == m2


def test_later_pdf_confirming_adjustments_causes_no_duplicates_or_date_shift():
    m, _ = apply_adjustments(seeded(), ADJ)
    confirmed = snap(codes(360)[1:] + ["9999"], date(2026, 10, 5), sha="new")
    m2, rep = apply_snapshot(m, confirmed)
    assert rep["status"] == "unchanged" and rep["added"] == [] and rep["removed"] == []
    m3, rep3 = apply_adjustments(m2, ADJ)
    assert rep3["status"] == "unchanged"
    new = [x for x in m3["members"] if x["security_code"] == "9999"]
    old = [x for x in m3["members"] if x["security_code"] == "1000"]
    assert len(new) == 1 and new[0]["effective_from"] == "2026-09-29"
    assert len(old) == 1 and old[0]["effective_to"] == "2026-09-30"


def test_adjustment_takes_precedence_over_pdf_derived_dates():
    # PDF (published 10/5) is applied first, then adjustments pull the dates back
    m = seeded()
    m, _ = apply_snapshot(m, snap(codes(360)[1:] + ["9999"], date(2026, 10, 5), sha="new"))
    assert next(x for x in m["members"] if x["security_code"] == "9999")["effective_from"] == "2026-10-05"
    m, rep = apply_adjustments(m, ADJ)
    assert next(x for x in m["members"] if x["security_code"] == "9999")["effective_from"] == "2026-09-29"
    u = Universe(m)
    assert not u.is_member("1000", date(2026, 10, 1))
    assert u.is_member("1000", date(2026, 9, 30))
    assert sum(1 for x in m["members"] if x["security_code"] == "1000") == 1


def test_pdf_published_after_effective_date_missing_added_code_is_a_removal():
    m, _ = apply_adjustments(seeded(), ADJ)
    m2, rep = apply_snapshot(m, snap(codes(360), date(2026, 10, 6), sha="later"))
    assert "9999" in rep["removed"]


def test_apply_adjustments_uses_previous_business_day_callable():
    m, _ = apply_adjustments(seeded(), ADJ[1:], previous_business_day=lambda d: date(2026, 9, 29))
    assert next(x for x in m["members"] if x["security_code"] == "1000")["effective_to"] == "2026-09-29"


def test_repo_state_adjustments_file_is_valid():
    adjs = load_adjustments(Path(__file__).parents[2] / "state" / "jpx400_adjustments.json")
    assert {(a["security_code"], a["action"], a["effective_date"]) for a in adjs} == {
        ("646A", "add", "2026-09-29"), ("9508", "remove", "2026-10-01"), ("642A", "add", "2026-10-01")}
    assert all(a["source_url"].startswith("https://www.jpx.co.jp/") for a in adjs)
