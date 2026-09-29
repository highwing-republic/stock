"""JPX 銘柄別信用取引残高（日次 PDF＋週末残高 PDF）の取得・解析・履歴化。

日次（2026-09-28 申込分から、営業日 16:00 頃に前営業日分）:
  https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/YYYYMMDD_mtall.pdf
  ファイル名の日付は「申込日」（=残高基準日）。実際の添付ディレクトリ名は変わりうるため、
  一覧ページ margin/01.html のリンクから探索する。
週末残高（旧フォーマット。フォールバック）: 同ページの syumatsuYYYYMMDD00.pdf。

PDF は pypdf のテキスト抽出で、数値の途中に空白が混入する（"119, 000" "470,0 00" "54 2,349,000"）。
そのため各行を「カンマ区切りの妥当な数値へ分割し直す」→「一般+制度=合計」の整合で検証、の方式で読む。
"""
from __future__ import annotations

import io
import itertools
import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, timedelta

from src.watch.sources.jpx400 import USER_AGENT, normalize_security_code

log = logging.getLogger(__name__)

INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
BASE_URL = "https://www.jpx.co.jp"


class MarginValidationError(Exception):
    pass


@dataclass(frozen=True)
class MarginRow:
    security_code: str
    buy_balance: int | None
    sell_balance: int | None
    buy_value: int | None
    sell_value: int | None
    general_buy: int | None
    general_sell: int | None
    institutional_buy: int | None
    institutional_sell: int | None


@dataclass(frozen=True)
class MarginSnapshot:
    as_of: date
    kind: str  # "daily" | "weekly"
    source_url: str
    rows: dict[str, MarginRow]


# ---------------------------------------------------------------- 数値行の解析

_NUM_RE = re.compile(r"^▲?\d{1,3}(?:,\d{3})*$")
_RATIO_RE = re.compile(r"^(?:\d+(?:\.\d+)?%|\*|-)$")
_AS_OF_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s*申込み現在")
_ISIN_PART = r"[A-Z]{2}[0-9A-Z ]{0,14}?"
_DAILY_SHARES_RE = re.compile(
    rf"(?<![0-9A-Za-z])(\d{{3}}[0-9A-Z]0)\s+{_ISIN_PART}\s*株\s*数\s*S\s*h\s*s\s*\.\s*(.*)$"
)
_DAILY_VALUE_RE = re.compile(
    rf"(?<![0-9A-Za-z])(\d{{3}}[0-9A-Z]0)\s+{_ISIN_PART}\s*金\s*額\s*V\s*a\s*l\s*\.\s*(.*)$"
)
_WEEKLY_RE = re.compile(r"(?<![0-9A-Za-z])(\d{3}[0-9A-Z]0)\s+([A-Z]{2}[0-9A-Z]*)\s*(.*)$")

_MAX_SEGMENTATIONS = 200
_MAX_FRAGMENTS_PER_NUMBER = 4


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"▲\s+", "▲", text)
    text = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", text)
    text = re.sub(r"\s+%", "%", text)
    return text.split()


def _segmentations(frags: list[str], k: int):
    """frags を連結して妥当な数値 k 個に分ける全パターン（分割の少ない順）。"""

    def rec(i: int, remaining: int):
        if remaining == 0:
            if i == len(frags):
                yield []
            return
        left = len(frags) - i
        if left < remaining:
            return
        for j in range(i + 1, min(len(frags), i + _MAX_FRAGMENTS_PER_NUMBER) + 1):
            if len(frags) - j < remaining - 1:
                break
            if any("▲" in f for f in frags[i + 1 : j]):
                break
            joined = "".join(frags[i:j])
            if not _NUM_RE.match(joined):
                continue
            for rest in rec(j, remaining - 1):
                yield [joined, *rest]

    yield from itertools.islice(rec(0, k), _MAX_SEGMENTATIONS)


def _to_int(text: str) -> int:
    return int(text.replace(",", "").replace("▲", "-"))


def _consistent(nums: list[int], sell_i: int, buy_i: int, gs: int, is_: int, gb: int, ib: int) -> bool:
    return nums[sell_i] == nums[gs] + nums[is_] and nums[buy_i] == nums[gb] + nums[ib]


def _parse_daily_numbers(rest: str) -> list[int] | None:
    """株数/金額の 14 フィールド: 売合計,前日比,上場比 | 買合計,前日比,上場比 | 売一般,比,売制度,比,買一般,比,買制度,比。

    返り値は上場比を除く 12 個 [売合計,比,買合計,比,売一般,比,売制度,比,買一般,比,買制度,比]。
    """
    toks = _tokens(rest)
    ratio_idx = [i for i, t in enumerate(toks) if _RATIO_RE.match(t)]
    if len(ratio_idx) != 2:
        return None
    a, b = ratio_idx
    seg1, seg2, seg3 = toks[:a], toks[a + 1 : b], toks[b + 1 :]
    for s1 in _segmentations(seg1, 2):
        for s2 in _segmentations(seg2, 2):
            for s3 in _segmentations(seg3, 8):
                nums = [_to_int(x) for x in (*s1, *s2, *s3)]
                if _consistent(nums, 0, 2, 4, 6, 8, 10):
                    return nums
    return None


def _parse_weekly_numbers(rest_tokens: list[str]) -> list[int] | None:
    """週末残高の 12 フィールド（daily と同じ並び、上場比なし）。"""
    for segmentation in _segmentations(rest_tokens, 12):
        nums = [_to_int(x) for x in segmentation]
        if _consistent(nums, 0, 2, 4, 6, 8, 10):
            return nums
    return None


def extract_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _parse_as_of(text: str) -> date:
    match = _AS_OF_RE.search(text)
    if not match:
        raise MarginValidationError("基準日（YYYY/M/D 申込み現在）が見つかりません")
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _check_failure_rate(detected: int, failed: int, label: str) -> None:
    if detected == 0:
        raise MarginValidationError(f"{label}: 銘柄行を 1 件も検出できません")
    if failed > max(5, int(detected * 0.02)):
        raise MarginValidationError(f"{label}: 行の解析失敗が多すぎます ({failed}/{detected})")


def parse_daily_text(text: str, *, source_url: str) -> MarginSnapshot:
    as_of = _parse_as_of(text)
    shares: dict[str, list[int]] = {}
    values: dict[str, list[int]] = {}
    detected = failed = 0
    for line in text.splitlines():
        m = _DAILY_SHARES_RE.search(line)
        if m:
            code = normalize_security_code(m.group(1))
            if not code:
                continue
            detected += 1
            nums = _parse_daily_numbers(m.group(2))
            if nums is None:
                failed += 1
                log.warning("daily margin: unparsable shares row %s", code)
            else:
                shares.setdefault(code, nums)
            continue
        m = _DAILY_VALUE_RE.search(line)
        if m:
            code = normalize_security_code(m.group(1))
            nums = _parse_daily_numbers(m.group(2)) if code else None
            if code and nums is not None:
                values.setdefault(code, nums)
    _check_failure_rate(detected, failed, "日次信用残")
    rows: dict[str, MarginRow] = {}
    for code, n in shares.items():
        v = values.get(code)
        rows[code] = MarginRow(
            security_code=code,
            buy_balance=n[2],
            sell_balance=n[0],
            buy_value=v[2] if v else None,
            sell_value=v[0] if v else None,
            general_buy=n[8],
            general_sell=n[4],
            institutional_buy=n[10],
            institutional_sell=n[6],
        )
    return MarginSnapshot(as_of=as_of, kind="daily", source_url=source_url, rows=rows)


def parse_daily_pdf(content: bytes, *, source_url: str) -> MarginSnapshot:
    return parse_daily_text(extract_pdf_text(content), source_url=source_url)


def parse_weekly_text(text: str, *, source_url: str) -> MarginSnapshot:
    as_of = _parse_as_of(text)
    rows: dict[str, MarginRow] = {}
    detected = failed = 0
    for line in text.splitlines():
        m = _WEEKLY_RE.search(unicodedata.normalize("NFKC", line))
        if not m:
            continue
        code = normalize_security_code(m.group(1))
        if not code:
            continue
        isin = m.group(2)
        rest = _tokens(m.group(3))
        while len(isin) < 12 and rest and re.fullmatch(r"[0-9A-Z]+", rest[0]) and len(isin) + len(rest[0]) <= 12:
            isin += rest.pop(0)
        if len(isin) != 12:
            continue
        detected += 1
        nums = _parse_weekly_numbers(rest)
        if nums is None:
            failed += 1
            log.warning("weekly margin: unparsable row %s", code)
            continue
        rows.setdefault(
            code,
            MarginRow(
                security_code=code,
                buy_balance=nums[2],
                sell_balance=nums[0],
                buy_value=None,
                sell_value=None,
                general_buy=nums[8],
                general_sell=nums[4],
                institutional_buy=nums[10],
                institutional_sell=nums[6],
            ),
        )
    _check_failure_rate(detected, failed, "週末信用残")
    return MarginSnapshot(as_of=as_of, kind="weekly", source_url=source_url, rows=rows)


def parse_weekly(content: bytes, *, source_url: str) -> MarginSnapshot:
    return parse_weekly_text(extract_pdf_text(content), source_url=source_url)


# ---------------------------------------------------------------- 検証・絞り込み

def validate(
    snapshot: MarginSnapshot,
    universe_codes: set[str],
    *,
    min_rows: int,
    min_universe_coverage: float,
) -> None:
    if len(snapshot.rows) < min_rows:
        raise MarginValidationError(f"信用残の行数 {len(snapshot.rows)} が下限 {min_rows} 未満")
    if not any((r.buy_balance or 0) > 0 for r in snapshot.rows.values()):
        raise MarginValidationError("信用買残がすべて 0 です")
    if universe_codes:
        covered = len(universe_codes & set(snapshot.rows))
        coverage = covered / len(universe_codes)
        if coverage < min_universe_coverage:
            raise MarginValidationError(
                f"JPX400 カバー率 {coverage:.3f} が下限 {min_universe_coverage} 未満"
            )


def filter_to(snapshot: MarginSnapshot, codes: set[str]) -> MarginSnapshot:
    return replace(snapshot, rows={c: r for c, r in snapshot.rows.items() if c in codes})


# ---------------------------------------------------------------- 取得

_DAILY_LINK_RE = re.compile(r'href="([^"]*?(\d{8})_mtall\.pdf)"')
_WEEKLY_LINK_RE = re.compile(r'href="([^"]*?syumatsu(\d{8})\d*\.pdf)"')


def _absolute(href: str) -> str:
    return href if href.startswith("http") else BASE_URL + (href if href.startswith("/") else "/" + href)


def discover_links(html: str) -> dict[str, list[str]]:
    """一覧ページから新しい順の PDF URL を返す。{"daily": [...], "weekly": [...]}"""
    def collect(regex: re.Pattern[str]) -> list[str]:
        found = {m.group(2): _absolute(m.group(1)) for m in regex.finditer(html)}
        return [found[k] for k in sorted(found, reverse=True)]

    return {"daily": collect(_DAILY_LINK_RE), "weekly": collect(_WEEKLY_LINK_RE)}


def _get(session, url: str, timeout: float):
    response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response


def fetch_latest(
    session=None,
    *,
    universe_codes: set[str],
    min_rows: int = 2000,
    min_universe_coverage: float = 0.9,
    prefer_daily: bool = True,
    timeout: float = 60,
    max_candidates: int = 2,
) -> MarginSnapshot:
    """日次 PDF を優先し、失敗（取得・解析・検証）したら週末残高へフォールバックする。"""
    import requests

    http = session or requests.Session()
    errors: list[str] = []
    try:
        html = _get(http, INDEX_URL, timeout).content.decode("utf-8", errors="replace")
        links = discover_links(html)
    except Exception as exc:  # noqa: BLE001 - 取得失敗は集約して報告
        raise MarginValidationError(f"信用残の一覧ページを取得できません: {exc}") from exc

    order = [("daily", parse_daily_pdf), ("weekly", parse_weekly)]
    if not prefer_daily:
        order.reverse()
    for kind, parser in order:
        for url in links[kind][:max_candidates]:
            try:
                content = _get(http, url, timeout).content
                snapshot = parser(content, source_url=url)
                validate(
                    snapshot,
                    universe_codes,
                    min_rows=min_rows,
                    min_universe_coverage=min_universe_coverage,
                )
                return snapshot
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kind} {url}: {exc}")
                log.warning("margin %s failed: %s", kind, exc)
    raise MarginValidationError("信用残を取得できませんでした: " + " / ".join(errors) if errors else "候補なし")


# ---------------------------------------------------------------- 履歴・表示

_ENTRY_FIELDS = (
    "buy_balance",
    "sell_balance",
    "buy_value",
    "sell_value",
    "general_buy",
    "general_sell",
    "institutional_buy",
    "institutional_sell",
)


def merge_history(history: dict, snapshot: MarginSnapshot, *, keep: int = 30) -> dict:
    """{code: [entry, ...]}（as_of 昇順、新しいものが末尾）へ snapshot を追記した新しい dict を返す。

    同一 as_of は 1 件に統合する（日次を優先。同種なら新しい方で置換）。
    """
    merged = {code: [dict(e) for e in entries] for code, entries in history.items()}
    as_of = snapshot.as_of.isoformat()
    for code, row in snapshot.rows.items():
        entries = merged.setdefault(code, [])
        entry = {"as_of": as_of, "kind": snapshot.kind}
        entry.update({f: getattr(row, f) for f in _ENTRY_FIELDS})
        existing = next((e for e in entries if e["as_of"] == as_of), None)
        if existing is None:
            entries.append(entry)
        elif not (existing["kind"] == "daily" and snapshot.kind == "weekly"):
            entries[entries.index(existing)] = entry
        entries.sort(key=lambda e: e["as_of"])
        if keep and len(entries) > keep:
            del entries[: len(entries) - keep]
    return merged


def _weekdays_before(day: date, n: int) -> date:
    while n > 0:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _pct(now: int | None, ref: int | None) -> float | None:
    if now is None or not ref:
        return None
    return round((now / ref - 1) * 100, 1)


def _ratio(buy: int | None, sell: int | None) -> float | None:
    if buy is None or not sell:
        return None
    return round(buy / sell, 2)


def margin_view(history_for_code: list[dict], *, surge_pct: float = 0.20, drop_pct: float = 0.10) -> dict | None:
    """最新エントリと比較基準（日次: 約 5 営業日前 / 週次: 前週）から表示用ビューを作る。

    *Pct は % 値（小数 1 桁）。surge_pct / drop_pct は比率（0.20 = 20%）。
    """
    if not history_for_code:
        return None
    entries = sorted(history_for_code, key=lambda e: e["as_of"])
    latest = entries[-1]
    latest_day = date.fromisoformat(latest["as_of"])
    target = (
        latest_day - timedelta(days=7)
        if latest.get("kind") == "weekly"
        else _weekdays_before(latest_day, 5)
    )
    earlier = [e for e in entries[:-1] if date.fromisoformat(e["as_of"]) <= target]
    ref = earlier[-1] if earlier else None

    buy, sell = latest.get("buy_balance"), latest.get("sell_balance")
    ratio = _ratio(buy, sell)
    buy_chg = _pct(buy, ref.get("buy_balance")) if ref else None
    sell_chg = _pct(sell, ref.get("sell_balance")) if ref else None
    ref_ratio = _ratio(ref.get("buy_balance"), ref.get("sell_balance")) if ref else None

    labels: list[str] = []
    if buy_chg is not None:
        if buy_chg <= -drop_pct * 100:
            labels.append("信用買残減少")
        if buy_chg >= surge_pct * 100:
            labels.append("信用買残急増")
    if ratio is not None and ref_ratio:
        if ratio <= ref_ratio * (1 - drop_pct):
            labels.append("信用倍率改善")
        if ratio >= ref_ratio * (1 + surge_pct):
            labels.append("信用倍率悪化")
    if sell_chg is not None and sell_chg >= surge_pct * 100:
        labels.append("売残増加")
    return {
        "asOf": latest["as_of"],
        "buyBalance": buy,
        "sellBalance": sell,
        "ratio": ratio,
        "buyChangePct": buy_chg,
        "sellChangePct": sell_chg,
        "labels": labels,
    }
