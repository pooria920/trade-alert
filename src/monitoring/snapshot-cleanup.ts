import { setImmediate as yieldLoop } from 'node:timers/promises';
import type { Logger } from 'pino';
import type { SnapshotRepository } from '../database/repositories/snapshot.repository.js';

export class SnapshotCleanup {
  private nextRun = 0;
  private task: Promise<void> | null = null;
  constructor(
    private readonly repository: SnapshotRepository,
    private readonly retentionMs: number,
    private readonly intervalMs: number,
    private readonly logger: Logger,
  ) {}

  runIfDue(now: number, signal: AbortSignal) {
    if (this.task || signal.aborted || now < this.nextRun) return;
    this.nextRun = now + this.intervalMs;
    this.task = this.clean(Math.max(0, now - this.retentionMs), signal)
      .catch((err: unknown) => {
        this.logger.error({ err }, 'Snapshot cleanup failed');
      })
      .finally(() => {
        this.task = null;
      });
  }

  private async clean(cutoff: number, signal: AbortSignal) {
    let removed = 0;
    while (!signal.aborted) {
      const count = this.repository.deleteExpired(cutoff);
      removed += count;
      if (count < 1000) break;
      await yieldLoop();
    }
    if (removed) this.logger.info({ removed }, 'Expired snapshots removed');
  }
  async wait() {
    await this.task;
  }
}
