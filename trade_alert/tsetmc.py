"""Observed TSETMC routes, cancellable HTTP and unverified book normalization."""

import asyncio
import random
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .market import Instrument, parse_book, parse_instrument, tehran_date
from .users import now_ms
from .validation import integer, loads, number, obj, text


class HttpError(Exception):
    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"TSETMC HTTP {status}")
        self.status, self.retry_after = status, retry_after


def normalize_persian(value: str) -> str:
    return value.replace("ي", "ی").replace("ك", "ک").strip()


def _array(value: object, key: str, maximum: int) -> list[dict[str, Any]]:
    items = obj(value)[key]
    if not isinstance(items, list) or len(items) > maximum:
        raise ValueError("Invalid response array")
    return [obj(item) for item in items]


def _retry_after(header: str | None, now: int) -> float | None:
    if header is None:
        return None
    try:
        seconds = float(header) if header.strip() else 0.0
        milliseconds = seconds * 1000
        if isfinite(milliseconds):
            return max(0, milliseconds)
    except ValueError:
        pass
    try:
        date = parsedate_to_datetime(header)
        if date.tzinfo is None:
            return None
        return max(0, date.timestamp() * 1000 - now)
    except ValueError, TypeError, OverflowError:
        return None


class TsetmcClient:
    def __init__(
        self,
        base_url: str,
        timeout_ms: int = 5000,
        max_retries: int = 2,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], int] = now_ms,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        parts = urlsplit(base_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Expected an HTTPS API base URL without credentials")
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = integer(timeout_ms, 1, 120_000)
        self.max_retries = integer(max_retries, 0, 3)
        self.now, self.jitter = now, jitter
        self.http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=self.timeout_ms / 1000,
            headers={"Accept": "application/json"},
        )
        self.closed = False
        self.active: set[asyncio.Task[Any]] = set()
        self.cooldown_until = 0.0
        self.cooldown_error: HttpError | None = None

    async def close(self) -> None:
        self.closed = True
        active = tuple(self.active)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        await self.http.aclose()

    async def _request(self, path: str) -> object:
        if self.closed:
            raise RuntimeError("Market client is closed")
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            for attempt in range(self.max_retries + 1):
                if self.closed or task.cancelling():
                    raise asyncio.CancelledError
                if self.cooldown_error and self.now() < self.cooldown_until:
                    raise self.cooldown_error
                try:
                    async with asyncio.timeout(self.timeout_ms / 1000) as deadline:
                        response = await self.http.get(f"{self.base_url}/{path}")
                    if deadline.expired():
                        raise TimeoutError
                    if self.closed or task.cancelling():
                        raise asyncio.CancelledError
                    if not response.is_success:
                        error = HttpError(
                            response.status_code,
                            _retry_after(
                                response.headers.get("retry-after"), self.now()
                            ),
                        )
                        if error.status == 429 or error.retry_after is not None:
                            until = self.now() + (
                                error.retry_after
                                if error.retry_after is not None
                                else 500
                            )
                            if until > self.cooldown_until:
                                self.cooldown_until, self.cooldown_error = until, error
                        raise error
                    return loads(response.text)
                except (
                    HttpError,
                    httpx.NetworkError,
                    httpx.TimeoutException,
                    httpx.RemoteProtocolError,
                    TimeoutError,
                ) as error:
                    if (
                        isinstance(error, HttpError)
                        and error.status not in (429, 500, 502, 503, 504)
                    ) or attempt >= self.max_retries:
                        raise
                    delay = max(
                        500 * 2**attempt * (1 + number(self.jitter(), 0, 1) * 0.25),
                        (error.retry_after or 0) if isinstance(error, HttpError) else 0,
                    )
                    if delay > 30_000:
                        raise
                    await asyncio.sleep(delay / 1000)
            raise AssertionError("Unreachable retry state")
        finally:
            self.active.discard(task)

    async def search(self, symbol: str) -> list[Instrument]:
        query = normalize_persian(text(symbol, 80))
        response = await self._request(
            f"Instrument/GetInstrumentSearch/{quote(query, safe='')}"
        )
        return [
            parse_instrument(
                dict(
                    insCode=row["insCode"],
                    symbol=normalize_persian(text(row["lVal18AFC"], 80)),
                    name=normalize_persian(text(row["lVal30"], 200)),
                )
            )
            for row in _array(response, "instrumentSearch", 2000)
        ]

    async def snapshot(self, instrument: Instrument) -> dict[str, Any]:
        item = parse_instrument(
            dict(
                insCode=instrument.ins_code,
                symbol=instrument.symbol,
                name=instrument.name,
            )
        )
        date = tehran_date(self.now())
        levels = []
        for row in _array(
            await self._request(f"BestLimits/{item.ins_code}"), "bestLimits", 100
        ):
            levels.append(
                dict(
                    rank=integer(row["number"], 1, 100),
                    bid=dict(
                        price=number(row["pMeDem"]),
                        volume=integer(row["qTitMeDem"]),
                        orderCount=integer(row["zOrdMeDem"]),
                    ),
                    ask=dict(
                        price=number(row["pMeOf"]),
                        volume=integer(row["qTitMeOf"]),
                        orderCount=integer(row["zOrdMeOf"]),
                    ),
                )
            )
        closing = obj(
            obj(
                await self._request(f"ClosingPrice/GetClosingPriceInfo/{item.ins_code}")
            )["closingPriceInfo"]
        )
        closing_date = integer(closing["dEven"], 19000101, 99991231)
        integer(closing["hEven"], 0, 235959)
        state = closing["instrumentState"]
        state_code = (
            None
            if state is None
            else text(obj(state)["cEtaval"], 20, trim=False, minimum=0).strip()
        )
        thresholds = []
        for row in _array(
            await self._request(
                f"MarketData/GetStaticThreshold/{item.ins_code}/{date}"
            ),
            "staticThreshold",
            1000,
        ):
            day = integer(row["dEven"], 19000101, 99991231)
            clock = integer(row["hEven"], 0, 235959)
            upper, lower = number(row["psGelStaMax"]), number(row["psGelStaMin"])
            if upper <= 0 or lower <= 0:
                raise ValueError("Invalid static threshold")
            if str(day) == date:
                thresholds.append((clock, upper, lower))
        limit = max(thresholds, key=lambda row: row[0]) if thresholds else None
        book = dict(
            instrument=dict(insCode=item.ins_code, symbol=item.symbol, name=item.name),
            timestamp=None,
            receivedAt=self.now(),
            completeness="unknown",
            verified=False,
            tradingStatus="open"
            if str(closing_date) == date and state_code == "A"
            else "unknown",
            limits=dict(
                insCode=item.ins_code, date=date, upper=limit[1], lower=limit[2]
            )
            if limit
            else None,
            levels=levels,
        )
        parse_book(book)
        return book
