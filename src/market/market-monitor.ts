import { setTimeout as delay } from 'node:timers/promises';
import type { Logger } from 'pino';
import type { SnapshotRepository } from '../database/repositories/snapshot.repository.js';
import type { SnapshotCleanup } from '../monitoring/snapshot-cleanup.js';
import type {
  MonitoringStatus,
  SymbolMonitoringStatus,
} from '../monitoring/monitoring-status.js';
import type { MarketService } from './market-service.js';
import type { MarketSchedule } from './market-schedule.js';
import type { Instrument, QueueSnapshot } from './market.types.js';
import { HttpError } from './tsetmc.client.js';

export interface MonitorOptions {
  pollIntervalMs: number;
  maxConcurrentRequests: number;
  requestTimeoutMs: number;
  maxAgeMs: number;
}

export class MarketMonitor {
  private task: Promise<void> | null = null;
  private abort = new AbortController();
  private cycling = false;
  private cycleCompletion: Promise<void> | null = null;
  private readonly symbols = new Map<string, SymbolMonitoringStatus>();
  private readonly status: Omit<MonitoringStatus, 'symbols' | 'freshSymbols'>;

  constructor(
    private readonly watched: () => Instrument[],
    private readonly market: MarketService,
    private readonly repository: SnapshotRepository,
    private readonly schedule: MarketSchedule,
    private readonly cleanup: SnapshotCleanup,
    private readonly options: MonitorOptions,
    private readonly logger: Logger,
    private readonly now: () => number = Date.now,
    private readonly random: () => number = Math.random,
    private readonly onSnapshot: (
      snapshot: QueueSnapshot,
      sessionId: string,
    ) => void = () => {},
  ) {
    this.status = {
      isRunning: false,
      lastProgressAt: null,
      isMarketOpen: false,
      scheduleConfigured: schedule.configured,
      forceActive: schedule.forceActive,
      monitoredSymbols: 0,
      pollIntervalMs: options.pollIntervalMs,
      maxAgeMs: options.maxAgeMs,
      lastPollingCycle: null,
      lastSuccessfulFetch: null,
      lastVerifiedSnapshot: null,
      consecutiveErrors: 0,
      rateLimitedUntil: 0,
      storedSnapshots: 0,
    };
  }

  start() {
    if (this.task) return;
    if (this.abort.signal.aborted) this.abort = new AbortController();
    this.status.isRunning = true;
    this.logger.info(
      {
        intervalMs: this.options.pollIntervalMs,
        scheduleConfigured: this.schedule.configured,
        forceActive: this.schedule.forceActive,
      },
      'Market monitor started',
    );
    this.task = this.loop(this.abort.signal).finally(() => {
      this.status.isRunning = false;
      this.task = null;
    });
  }

  async stop() {
    this.abort.abort();
    await this.task;
    await this.cycleCompletion;
    await this.cleanup.wait();
  }

  getStatus(): Readonly<MonitoringStatus> {
    const now = this.now();
    const freshSymbols = [...this.symbols.values()].filter(
      (state) =>
        state.lastResult === 'fresh-data' &&
        state.consecutiveErrors === 0 &&
        state.lastVerifiedSnapshot !== null &&
        now - state.lastVerifiedSnapshot >= 0 &&
        now - state.lastVerifiedSnapshot <= this.options.maxAgeMs,
    ).length;
    return structuredClone({
      ...this.status,
      isMarketOpen: this.schedule.isOpen(now),
      freshSymbols,
      symbols: [...this.symbols.values()],
    });
  }

  private async loop(signal: AbortSignal) {
    try {
      while (!signal.aborted) {
        try {
          await this.pollOnce(signal);
        } catch (err) {
          this.status.consecutiveErrors++;
          if (this.shouldLog(this.status.consecutiveErrors))
            this.logger.error({ err }, 'Market cycle failed');
        }
        this.cleanup.runIfDue(this.now(), signal);
        try {
          await delay(this.options.pollIntervalMs, undefined, { signal });
        } catch (err) {
          if (!signal.aborted) throw err;
        }
      }
    } catch (err) {
      this.status.consecutiveErrors++;
      this.logger.error({ err }, 'Market monitor stopped unexpectedly');
    }
  }

  async pollOnce(signal = this.abort.signal): Promise<void> {
    if (this.cycling || signal.aborted) return;
    this.cycling = true;
    this.status.lastProgressAt = this.now();
    let finishCycle: (() => void) | undefined;
    this.cycleCompletion = new Promise<void>((resolve) => {
      finishCycle = resolve;
    });
    try {
      const instruments = [
        ...new Map(
          this.watched().map((instrument) => [instrument.insCode, instrument]),
        ).values(),
      ];
      this.status.monitoredSymbols = instruments.length;
      const active = new Set(
        instruments.map((instrument) => instrument.insCode),
      );
      for (const insCode of this.symbols.keys())
        if (!active.has(insCode)) this.symbols.delete(insCode);
      const open = this.schedule.isOpen(this.now());
      if (open !== this.status.isMarketOpen)
        this.logger.info(
          { isMarketOpen: open },
          'Configured market session changed',
        );
      this.status.isMarketOpen = open;
      if (!open || this.now() < this.status.rateLimitedUntil) return;
      this.status.lastPollingCycle = this.now();
      let index = 0;
      let failures = 0;
      const worker = async () => {
        while (
          !signal.aborted &&
          this.now() >= this.status.rateLimitedUntil &&
          this.schedule.isOpen(this.now())
        ) {
          const instrument = instruments[index++];
          if (!instrument) break;
          const previous = this.symbols.get(instrument.insCode);
          if (previous && this.now() < previous.retryAt) continue;
          const state: SymbolMonitoringStatus = previous ?? {
            insCode: instrument.insCode,
            consecutiveErrors: 0,
            lastSuccessfulFetch: null,
            lastVerifiedSnapshot: null,
            lastResult: 'none',
            reason: null,
            retryAt: 0,
          };
          this.symbols.set(instrument.insCode, state);
          let fetched = false;
          try {
            const book = await this.market.snapshot(
              instrument,
              AbortSignal.any([
                signal,
                AbortSignal.timeout(this.options.requestTimeoutMs),
              ]),
            );
            if (signal.aborted) break;
            fetched = true;
            const collectedAt = this.now();
            state.lastSuccessfulFetch = collectedAt;
            this.status.lastSuccessfulFetch = collectedAt;
            state.lastResult = 'invalid-data';
            const detection = this.market.detect(book, collectedAt);
            if (!detection.valid) {
              state.lastResult =
                detection.reason === 'stale-data'
                  ? 'stale-data'
                  : 'invalid-data';
              throw new Error(detection.reason);
            }
            // Membership/session can change while HTTP is in flight; do not store an orphan update.
            if (
              !this.schedule.isOpen(collectedAt) ||
              !this.watched().some(
                (item) => item.insCode === instrument.insCode,
              )
            )
              continue;
            const sessionId = this.schedule.sessionId(
              detection.snapshot.timestamp,
            );
            if (
              !sessionId ||
              sessionId !== this.schedule.sessionId(collectedAt)
            )
              continue;
            const inserted = this.repository.save(
              detection.snapshot,
              collectedAt,
              sessionId,
            );
            if (inserted) {
              this.status.storedSnapshots++;
              try {
                this.onSnapshot(detection.snapshot, sessionId);
              } catch (err) {
                this.logger.error(
                  { err },
                  'Alert evaluation failed; collection continues',
                );
              }
            }
            if (state.consecutiveErrors)
              this.logger.info(
                {
                  insCode: instrument.insCode,
                  previousErrors: state.consecutiveErrors,
                },
                'Market data recovered',
              );
            state.consecutiveErrors = 0;
            state.reason = null;
            state.retryAt = 0;
            state.lastResult = 'fresh-data';
            state.lastVerifiedSnapshot = detection.snapshot.timestamp;
            this.status.lastVerifiedSnapshot = Math.max(
              this.status.lastVerifiedSnapshot ?? 0,
              detection.snapshot.timestamp,
            );
          } catch (err) {
            if (signal.aborted) break;
            if (!fetched) state.lastResult = 'transport-error';
            failures++;
            state.consecutiveErrors++;
            const reason =
              err instanceof Error ? err.message : 'Unknown failure';
            const changed = reason !== state.reason;
            state.reason = reason;
            const backoff = Math.min(
              300_000,
              this.options.pollIntervalMs *
                2 ** Math.min(state.consecutiveErrors - 1, 10) *
                (1 + this.random() * 0.25),
            );
            state.retryAt = this.now() + backoff;
            if (
              err instanceof HttpError &&
              (err.status === 429 || err.retryAfter !== null)
            ) {
              this.status.rateLimitedUntil = Math.max(
                this.status.rateLimitedUntil,
                this.now() + (err.retryAfter ?? backoff),
              );
              state.retryAt = Math.max(
                state.retryAt,
                this.status.rateLimitedUntil,
              );
            }
            if (changed || this.shouldLog(state.consecutiveErrors))
              this.logger.warn(
                {
                  err,
                  insCode: instrument.insCode,
                  consecutiveErrors: state.consecutiveErrors,
                  retryAt: state.retryAt,
                  result: state.lastResult,
                },
                'Market symbol failed; snapshot not stored',
              );
          } finally {
            this.status.lastProgressAt = this.now();
          }
        }
      };
      await Promise.all(
        Array.from(
          {
            length: Math.min(
              this.options.maxConcurrentRequests,
              instruments.length,
            ),
          },
          worker,
        ),
      );
      if (failures) this.status.consecutiveErrors++;
      else if (
        [...this.symbols.values()].every(
          (state) => state.consecutiveErrors === 0,
        )
      )
        this.status.consecutiveErrors = 0;
      this.logger.debug(
        { symbols: instruments.length, failures },
        'Market cycle completed',
      );
    } finally {
      this.cycling = false;
      finishCycle?.();
    }
  }

  private shouldLog(count: number) {
    return count > 0 && (count & (count - 1)) === 0;
  }
}
