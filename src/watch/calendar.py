"""営業日と event_market_date。営業日 = ベンチマークに価格がある日（それより先は平日−祝日）。"""
from __future__ import annotations

import bisect
import re
from datetime import date, timedelta
from typing import Iterable

_DT = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ]+(\d{1,2}):(\d{2}))?")


def to_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class MarketCalendar:
    def __init__(self, bench_dates: Iterable = (), extra_holidays: Iterable = (), market_close: str = "15:30"):
        self.dates = sorted({to_date(x) for x in bench_dates})
        self._set = set(self.dates)
        self.holidays = {to_date(x) for x in extra_holidays}
        hh, mm = str(market_close).split(":")
        self.close_minutes = int(hh) * 60 + int(mm)

    @property
    def first(self) -> date | None:
        return self.dates[0] if self.dates else None

    @property
    def last(self) -> date | None:
        return self.dates[-1] if self.dates else None

    def is_business_day(self, day: date) -> bool:
        if self.dates and self.dates[0] <= day <= self.dates[-1]:
            return day in self._set
        return day.weekday() < 5 and day not in self.holidays

    def next_business_day(self, day: date) -> date:
        """day より後の最初の営業日。"""
        cur = day + timedelta(days=1)
        for _ in range(60):
            if self.is_business_day(cur):
                return cur
            cur += timedelta(days=1)
        raise ValueError(f"no business day after {day}")

    def prev_business_day(self, day: date) -> date:
        cur = day - timedelta(days=1)
        for _ in range(60):
            if self.is_business_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise ValueError(f"no business day before {day}")

    def business_days(self, start: date, end: date) -> list[date]:
        i, j = bisect.bisect_left(self.dates, start), bisect.bisect_right(self.dates, end)
        return self.dates[i:j]

    def event_market_date(self, submit_datetime: str | None) -> date | None:
        """EDINET の提出日時(JST)から Day0（最初に市場が反応できる営業日）を決める。"""
        match = _DT.search(str(submit_datetime or ""))
        if not match:
            return None
        y, m, d, hh, mm = match.groups()
        day = date(int(y), int(m), int(d))
        minutes = int(hh) * 60 + int(mm) if hh is not None else 0
        if self.is_business_day(day) and minutes < self.close_minutes:
            return day
        return self.next_business_day(day)
