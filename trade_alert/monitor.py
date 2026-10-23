"""One bounded collection loop for the authorized private/channel instrument union."""

import asyncio
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .cleanup import SnapshotCleanup
from .evaluators import QueueSnapshot
from .market import Instrument
from .market_service import MarketService
from .schedule import MarketSchedule
from .snapshots import SnapshotRepository
from .tsetmc import HttpError
from .users import now_ms
from .validation import integer, number


@dataclass(frozen=True, slots=True)
class MonitorOptions:
    poll_interval_ms: int = 10_000
    max_concurrent_requests: int = 3
    request_timeout_ms: int = 5000
    max_age_ms: int = 30_000

    def __post_init__(self) -> None:
        integer(self.poll_interval_ms, 1)
        integer(self.max_concurrent_requests, 1, 10)
        integer(self.request_timeout_ms, 1, 120_000)
        integer(self.max_age_ms, 1, 300_000)


@dataclass(slots=True)
class SymbolStatus:
    ins_code: str
    consecutive_errors: int = 0
    last_successful_fetch: int | None = None
    last_verified_snapshot: int | None = None
    last_result: str = "none"
    reason: str | None = None
    retry_at: float = 0


@dataclass(slots=True)
class MonitorStatus:
    is_running: bool = False
    last_progress_at: int | None = None
    is_market_open: bool = False
    monitored_symbols: int = 0
    last_polling_cycle: int | None = None
    last_successful_fetch: int | None = None
    last_verified_snapshot: int | None = None
    consecutive_errors: int = 0
    rate_limited_until: float = 0
    stored_snapshots: int = 0


def _sparse(count: int) -> bool:
    return count > 0 and count & (count - 1) == 0


class MarketMonitor:
    def __init__(
        self,
        watched: Callable[[], Sequence[Instrument]],
        market: MarketService,
        repository: SnapshotRepository,
        schedule: MarketSchedule,
        cleanup: SnapshotCleanup,
        options: MonitorOptions,
        on_snapshot: Callable[[QueueSnapshot, str], None],
        *,
        logger: logging.Logger | None = None,
        now: Callable[[], int] = now_ms,
        jitter: Callable[[], float] = random.random,
        before_cycle: Callable[[], None] | None = None,
        on_unavailable: Callable[[str, str], None] | None = None,
    ) -> None:
        self.watched, self.market, self.repository = watched, market, repository
        self.schedule, self.cleanup, self.options = schedule, cleanup, options
        self.on_snapshot, self.now, self.jitter = on_snapshot, now, jitter
        self.before_cycle = before_cycle
        self.on_unavailable = on_unavailable
        self.logger = logger or logging.getLogger(__name__)
        self.status = MonitorStatus()
        self.symbols: dict[str, SymbolStatus] = {}
        self.task: asyncio.Task[None] | None = None
        self.workers: list[asyncio.Task[None]] = []
        self.stop_event = asyncio.Event()
        self.cycle_done = asyncio.Event()
        self.cycle_done.set()

    def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.stop_event = asyncio.Event()
        self.status.is_running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self.workers:
            task.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        await self.cycle_done.wait()
        await self.market.cancel_pending()
        await self.cleanup.wait()

    def get_status(self) -> dict[str, Any]:
        now = self.now()
        fresh = sum(
            s.last_result == "fresh-data"
            and s.consecutive_errors == 0
            and s.last_verified_snapshot is not None
            and 0 <= now - s.last_verified_snapshot <= self.options.max_age_ms
            for s in self.symbols.values()
        )
        return asdict(self.status) | dict(
            is_market_open=self.schedule.is_open(now),
            schedule_configured=self.schedule.configured,
            force_active=self.schedule.force_active,
            fresh_symbols=fresh,
            poll_interval_ms=self.options.poll_interval_ms,
            max_age_ms=self.options.max_age_ms,
            symbols=[asdict(s) for s in self.symbols.values()],
        )

    async def _loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    await self.poll_once()
                except Exception:
                    self.status.consecutive_errors += 1
                    if _sparse(self.status.consecutive_errors):
                        self.logger.error("Market cycle failed")
                self.cleanup.run_if_due(self.now(), self.stop_event)
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.options.poll_interval_ms / 1000
                    )
                except TimeoutError:
                    pass
        except Exception:
            self.status.consecutive_errors += 1
            self.logger.error("Market monitor stopped unexpectedly")
        finally:
            self.status.is_running = False

    async def poll_once(self) -> None:
        if not self.cycle_done.is_set() or self.stop_event.is_set():
            return
        self.cycle_done.clear()
        self.status.last_progress_at = self.now()
        try:
            if self.before_cycle is not None:
                self.before_cycle()
            instruments = list({i.ins_code: i for i in self.watched()}.values())
            active = {i.ins_code for i in instruments}
            self.symbols = {
                key: state for key, state in self.symbols.items() if key in active
            }
            self.status.monitored_symbols = len(instruments)
            opened = self.schedule.is_open(self.now())
            if opened != self.status.is_market_open:
                self.logger.info(
                    "Configured market session changed",
                    extra={"is_market_open": opened},
                )
            self.status.is_market_open = opened
            if not opened or self.now() < self.status.rate_limited_until:
                if self.on_unavailable is not None:
                    for instrument in instruments:
                        self.on_unavailable(
                            instrument.ins_code,
                            "closed" if not opened else "unavailable",
                        )
                return
            self.status.last_polling_cycle = self.now()
            queue = iter(instruments)
            failures = 0

            async def worker() -> None:
                nonlocal failures
                while (
                    not self.stop_event.is_set()
                    and self.now() >= self.status.rate_limited_until
                    and self.schedule.is_open(self.now())
                ):
                    instrument = next(queue, None)
                    if instrument is None:
                        break
                    state = self.symbols.setdefault(
                        instrument.ins_code, SymbolStatus(instrument.ins_code)
                    )
                    if self.now() < state.retry_at:
                        continue
                    fetched = False
                    reason = "transport-error"
                    try:
                        async with asyncio.timeout(
                            self.options.request_timeout_ms / 1000
                        ) as deadline:
                            book = await self.market.snapshot(instrument)
                        if deadline.expired():
                            raise TimeoutError
                        if self.stop_event.is_set():
                            break
                        fetched = True
                        collected = self.now()
                        state.last_successful_fetch = collected
                        self.status.last_successful_fetch = collected
                        state.last_result = "invalid-data"
                        detection = self.market.detect(book, collected)
                        if not detection.valid or detection.snapshot is None:
                            reason = detection.reason or "invalid-data"
                            state.last_result = (
                                "stale-data"
                                if reason == "stale-data"
                                else "invalid-data"
                            )
                            raise ValueError("Rejected market evidence")
                        snapshot = detection.snapshot
                        if not self.schedule.is_open(collected) or not any(
                            i.ins_code == instrument.ins_code for i in self.watched()
                        ):
                            continue
                        session = self.schedule.session_id(snapshot.timestamp)
                        if session is None or session != self.schedule.session_id(
                            collected
                        ):
                            continue
                        reason = "persistence-error"
                        if self.repository.save(snapshot, collected, session):
                            self.status.stored_snapshots += 1
                            try:
                                self.on_snapshot(snapshot, session)
                            except Exception:
                                self.logger.error(
                                    "Alert evaluation failed; collection continues"
                                )
                        if state.consecutive_errors:
                            self.logger.info(
                                "Market data recovered",
                                extra={
                                    "ins_code": instrument.ins_code,
                                    "previous_errors": state.consecutive_errors,
                                },
                            )
                        state.consecutive_errors = 0
                        state.reason, state.retry_at = None, 0
                        state.last_result = "fresh-data"
                        state.last_verified_snapshot = snapshot.timestamp
                        self.status.last_verified_snapshot = max(
                            self.status.last_verified_snapshot or 0, snapshot.timestamp
                        )
                    except asyncio.CancelledError:
                        if not self.stop_event.is_set():
                            raise
                        return
                    except Exception as error:
                        if self.stop_event.is_set():
                            break
                        if not fetched:
                            state.last_result = "transport-error"
                            reason = (
                                f"HTTP_{error.status}"
                                if isinstance(error, HttpError)
                                else "timeout"
                                if isinstance(error, TimeoutError)
                                else "transport-error"
                            )
                        failures += 1
                        state.consecutive_errors += 1
                        changed = reason != state.reason
                        if self.on_unavailable is not None:
                            self.on_unavailable(instrument.ins_code, reason)
                        state.reason = reason
                        backoff = min(
                            300_000,
                            self.options.poll_interval_ms
                            * 2 ** min(state.consecutive_errors - 1, 10)
                            * (1 + number(self.jitter(), 0, 1) * 0.25),
                        )
                        state.retry_at = self.now() + backoff
                        if isinstance(error, HttpError) and (
                            error.status == 429 or error.retry_after is not None
                        ):
                            self.status.rate_limited_until = max(
                                self.status.rate_limited_until,
                                self.now()
                                + (
                                    error.retry_after
                                    if error.retry_after is not None
                                    else backoff
                                ),
                            )
                            state.retry_at = max(
                                state.retry_at, self.status.rate_limited_until
                            )
                        if changed or _sparse(state.consecutive_errors):
                            self.logger.warning(
                                "Market symbol failed; snapshot not stored",
                                extra={
                                    "ins_code": instrument.ins_code,
                                    "reason": reason,
                                    "consecutive_errors": state.consecutive_errors,
                                    "retry_at": state.retry_at,
                                    "result": state.last_result,
                                },
                            )
                    finally:
                        self.status.last_progress_at = self.now()

            self.workers = [
                asyncio.create_task(worker())
                for _ in range(
                    min(len(instruments), self.options.max_concurrent_requests)
                )
            ]
            try:
                await asyncio.gather(*self.workers)
            finally:
                for task in self.workers:
                    task.cancel()
                await asyncio.gather(*self.workers, return_exceptions=True)
                self.workers = []
            if failures:
                self.status.consecutive_errors += 1
            elif all(s.consecutive_errors == 0 for s in self.symbols.values()):
                self.status.consecutive_errors = 0
        finally:
            self.cycle_done.set()
