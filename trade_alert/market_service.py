"""One in-flight registry for on-demand, batch and scheduled market requests."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .market import Detection, Instrument, QueueDetector, parse_book, parse_instrument
from .validation import obj


class MarketService:
    def __init__(
        self,
        fetch: Callable[[Instrument], Awaitable[object]],
        search: Callable[[str], Awaitable[list[Instrument]]],
        max_age_ms: int = 30_000,
    ) -> None:
        self.fetch, self._search = fetch, search
        self.detector = QueueDetector(max_age_ms)
        self.pending: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.closed = False

    async def search(self, symbol: str) -> list[Instrument]:
        if self.closed:
            raise RuntimeError("Market service is closed")
        return await self._search(symbol)

    async def _fetch(self, instrument: Instrument) -> dict[str, Any]:
        value = obj(await self.fetch(instrument))
        book = parse_book(value)
        if book.instrument.ins_code != instrument.ins_code:
            raise ValueError("Market instrument mismatch")
        return value

    def _finished(self, ins_code: str, task: asyncio.Task[dict[str, Any]]) -> None:
        if self.pending.get(ins_code) is task:
            del self.pending[ins_code]
        if not task.cancelled():
            task.exception()  # Retrieve failures even after all consumers cancel.

    async def snapshot(self, value: Instrument) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("Market service is closed")
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError
        instrument = parse_instrument(
            dict(insCode=value.ins_code, symbol=value.symbol, name=value.name)
        )
        pending = self.pending.get(instrument.ins_code)
        # A completed callback may not have run yet. Completed/failed results are not a cache.
        if pending is not None and pending.done():
            self._finished(instrument.ins_code, pending)
            pending = None
        creator = pending is None
        if pending is None:
            pending = asyncio.create_task(self._fetch(instrument))
            self.pending[instrument.ins_code] = pending
            pending.add_done_callback(
                lambda task: self._finished(instrument.ins_code, task)
            )
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            if creator:
                # The creator owns the native request cancellation. Keep its entry
                # until it settles, even if a provider ignores cancellation.
                pending.cancel()
            raise

    async def snapshots(
        self, instruments: Sequence[Instrument]
    ) -> list[dict[str, Any]]:
        unique = {i.ins_code: i for i in instruments}
        return list(await asyncio.gather(*(self.snapshot(i) for i in unique.values())))

    def detect(self, book: object, now: int | None = None) -> Detection:
        return self.detector.detect(book, now)

    async def queue(self, instrument: Instrument) -> Detection:
        return self.detect(await self.snapshot(instrument))

    async def close(self) -> None:
        self.closed = True
        await self.cancel_pending()

    async def cancel_pending(self) -> None:
        """Join native requests during application/monitor shutdown."""
        tasks = tuple(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
