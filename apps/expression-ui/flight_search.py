#!/usr/bin/env python3
"""Bounded, read-only Google Flights lookup for the Daily voice assistant."""

from __future__ import annotations

import concurrent.futures
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo


GOOGLE_FLIGHTS_URL = "https://www.google.com/travel/flights"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DAYS = 7
DEFAULT_TIMEOUT_SECONDS = max(
    5.0,
    float(os.environ.get("RIVERBANK_FLIGHT_HTTP_TIMEOUT", "18")),
)
DEFAULT_WORKERS = max(
    1,
    min(DEFAULT_DAYS, int(os.environ.get("RIVERBANK_FLIGHT_WORKERS", "4"))),
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class FlightQuote:
    travel_date: date
    price_cny: int
    summary: str
    source_url: str


@dataclass(frozen=True)
class FlightSearchResult:
    origin: str
    destination: str
    start_date: date
    end_date: date
    quotes: tuple[FlightQuote, ...]
    failed_dates: tuple[date, ...]
    queried_at: datetime

    @property
    def cheapest(self) -> FlightQuote:
        return min(self.quotes, key=lambda quote: (quote.price_cny, quote.travel_date))


class _FlightLabelParser(HTMLParser):
    """Collect accessible flight result labels from server-rendered HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.labels: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key == "aria-label" and value and value.startswith("起价："):
                self.labels.append(value)


_FLIGHT_PRICE_RE = re.compile(r"起价：([\d,]+)\s*人民币。\s*(.+?)\s*选择航班")
_ROUTE_PATTERNS = (
    re.compile(
        r"从(?P<origin>[\u4e00-\u9fffA-Za-z]{2,14}?)"
        r"(?:飞往|飞到|前往|到|飞|去)"
        r"(?P<destination>[\u4e00-\u9fffA-Za-z]{2,14}?)"
        r"(?=的?(?:机票|航班)|(?:未来|接下来|近|这)?(?:一周|七天|7天)|[，。,.？?]|$)"
    ),
    re.compile(
        r"(?P<origin>[\u4e00-\u9fffA-Za-z]{2,14}?)"
        r"(?:飞往|飞到|前往|到|飞)"
        r"(?P<destination>[\u4e00-\u9fffA-Za-z]{2,14}?)"
        r"(?=的?(?:机票|航班)|(?:未来|接下来|近|这)?(?:一周|七天|7天)|[，。,.？?]|$)"
    ),
)
_ROUTE_PREFIXES = (
    "请帮我查询",
    "请帮我查",
    "帮我查询",
    "帮我查一下",
    "帮我查",
    "查询一下",
    "查一下",
    "查询",
    "查",
    "看看",
)
_CITY_CORRECTIONS = {
    "下门": "厦门",
    "夏门": "厦门",
    "廈門": "厦门",
    "深证": "深圳",
    "深正": "深圳",
}
_TRANSCRIPT_TRANSLATION = str.maketrans(
    {
        "從": "从",
        "機": "机",
        "價": "价",
        "進": "近",
        "門": "门",
        "飛": "飞",
        "廈": "厦",
        "錢": "钱",
        "幫": "帮",
        "這": "这",
    }
)


def _normalize_transcript(text: str) -> str:
    return re.sub(r"\s+", "", text).translate(_TRANSCRIPT_TRANSLATION)


def is_flight_price_request(text: str) -> bool:
    compact = _normalize_transcript(text)
    return bool(
        ("机票" in compact or "航班" in compact)
        and any(word in compact for word in ("价格", "票价", "便宜", "最低", "多少钱", "多少錢"))
    )


def _clean_city(value: str) -> str:
    city = re.sub(r"\s+", "", value).strip("，。,.？?")
    for prefix in _ROUTE_PREFIXES:
        if city.startswith(prefix):
            city = city[len(prefix) :]
            break
    city = city.removeprefix("我想").removeprefix("我要")
    city = _CITY_CORRECTIONS.get(city, city)
    return city.removesuffix("市")


def extract_flight_route(text: str) -> tuple[str, str] | None:
    compact = _normalize_transcript(text)
    for pattern in _ROUTE_PATTERNS:
        match = pattern.search(compact)
        if not match:
            continue
        origin = _clean_city(match.group("origin"))
        destination = _clean_city(match.group("destination"))
        if origin and destination and origin != destination:
            return origin, destination
    return None


def _query_url(origin: str, destination: str, travel_date: date) -> str:
    spoken_date = travel_date.strftime("%B") + f" {travel_date.day} {travel_date.year}"
    query = f"one-way flights from {origin} to {destination} on {spoken_date}"
    return GOOGLE_FLIGHTS_URL + "?" + urllib.parse.urlencode(
        {
            "q": query,
            "hl": "zh-CN",
            "curr": "CNY",
            "ucbcb": "1",
        }
    )


def _fetch_daily_quote(
    origin: str,
    destination: str,
    travel_date: date,
    timeout_seconds: float,
) -> FlightQuote:
    url = _query_url(origin, destination, travel_date)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        page = response.read().decode("utf-8", errors="replace")
    parser = _FlightLabelParser()
    parser.feed(page)
    matches: list[tuple[int, str]] = []
    for label in parser.labels:
        match = _FLIGHT_PRICE_RE.search(label)
        if match:
            matches.append((int(match.group(1).replace(",", "")), match.group(2)))
    if not matches:
        raise RuntimeError("Google Flights page contained no visible fare results")
    price, summary = min(matches, key=lambda item: item[0])
    return FlightQuote(
        travel_date=travel_date,
        price_cny=price,
        summary=summary.strip(),
        source_url=url,
    )


def search_next_week(
    origin: str,
    destination: str,
    *,
    today: date | None = None,
    days: int = DEFAULT_DAYS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FlightSearchResult:
    """Compare the next natural days concurrently and return visible fares."""

    if days < 1 or days > 14:
        raise ValueError("days must be between 1 and 14")
    base = today or datetime.now(SHANGHAI_TZ).date()
    travel_dates = [base + timedelta(days=offset) for offset in range(1, days + 1)]
    quotes: list[FlightQuote] = []
    failed: list[date] = []
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(DEFAULT_WORKERS, days)) as pool:
        futures = {
            pool.submit(
                _fetch_daily_quote,
                origin,
                destination,
                travel_date,
                timeout_seconds,
            ): travel_date
            for travel_date in travel_dates
        }
        for future in concurrent.futures.as_completed(futures):
            travel_date = futures[future]
            try:
                quotes.append(future.result())
            except Exception:
                failed.append(travel_date)
    # One short retry absorbs transient proxy/Google edge failures without ever
    # entering an unbounded browser loop.
    retry_dates = list(failed)
    failed = []
    if retry_dates:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(2, len(retry_dates))
        ) as pool:
            futures = {
                pool.submit(
                    _fetch_daily_quote,
                    origin,
                    destination,
                    travel_date,
                    timeout_seconds,
                ): travel_date
                for travel_date in retry_dates
            }
            for future in concurrent.futures.as_completed(futures):
                travel_date = futures[future]
                try:
                    quotes.append(future.result())
                except Exception:
                    failed.append(travel_date)
    if not quotes:
        elapsed = time.monotonic() - started
        raise RuntimeError(f"all {days} fare pages failed after {elapsed:.1f}s")
    return FlightSearchResult(
        origin=origin,
        destination=destination,
        start_date=travel_dates[0],
        end_date=travel_dates[-1],
        quotes=tuple(sorted(quotes, key=lambda quote: quote.travel_date)),
        failed_dates=tuple(sorted(failed)),
        queried_at=datetime.now(SHANGHAI_TZ),
    )


def format_spoken_result(result: FlightSearchResult) -> str:
    cheapest = result.cheapest
    date_text = f"{cheapest.travel_date.month}月{cheapest.travel_date.day}日"
    range_text = (
        f"{result.start_date.month}月{result.start_date.day}日到"
        f"{result.end_date.month}月{result.end_date.day}日"
    )
    short_summary = cheapest.summary.split("。")[0].strip()
    coverage = ""
    if result.failed_dates:
        coverage = f"这次成功比较了{len(result.quotes)}天，另有{len(result.failed_dates)}天页面没有正常返回。"
    return (
        f"按一名成人、经济舱、单程，我比较了{range_text}。"
        f"目前最低可见价格是{date_text}的{cheapest.price_cny}元，{short_summary}。"
        f"{coverage}数据来自 Google Flights，查询时间是"
        f"{result.queried_at.hour}点{result.queried_at.minute:02d}分，票价随时可能变化。"
    )
