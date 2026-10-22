"""Bounded snapshot retention, invoked by the monitor even outside sessions."""

import asyncio
import logging

from .snapshots import SnapshotRepository
from .validation import integer


class SnapshotCleanup:
    def __init__(
        self,
        repository: SnapshotRepository,
        retention_ms: int,
        interval_ms: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.retention_ms = integer(retention_ms)
        self.interval_ms = integer(interval_ms, 1)
        self.logger = logger or logging.getLogger(__name__)
        self.next_run = 0
        self.task: asyncio.Task[None] | None = None

    def run_if_due(self, now: int, stop: asyncio.Event) -> None:
        now = integer(now)
        if (
            (self.task is not None and not self.task.done())
            or stop.is_set()
            or now < self.next_run
        ):
            return
        self.next_run = now + self.interval_ms
        self.task = asyncio.create_task(
            self._clean(max(0, now - self.retention_ms), stop)
        )

    async def _clean(self, cutoff: int, stop: asyncio.Event) -> None:
        removed = 0
        try:
            while not stop.is_set():
                count = self.repository.delete_expired(cutoff)
                removed += count
                if count < 1000:
                    break
                await asyncio.sleep(0)
            if removed:
                self.logger.info(
                    "Expired snapshots removed", extra={"removed": removed}
                )
        except Exception:
            self.logger.error("Snapshot cleanup failed")

    async def wait(self) -> None:
        """Set the shared stop event, then join before closing the database."""
        if self.task is not None:
            await self.task
