import {
  instrumentSchema,
  orderBookSnapshotSchema,
  type Instrument,
  type MarketDataProvider,
  type OrderBookSnapshot,
} from './market.types.js';
import { QueueDetector } from './queue-detector.js';

export class MarketService {
  private readonly pending = new Map<string, Promise<OrderBookSnapshot>>();
  private readonly detector: QueueDetector;
  constructor(
    private readonly client: MarketDataProvider,
    maxAgeMs = 30_000,
  ) {
    this.detector = new QueueDetector(maxAgeMs);
  }
  search(symbol: string) {
    return this.client.search(symbol);
  }

  snapshot(
    value: Instrument,
    signal?: AbortSignal,
  ): Promise<OrderBookSnapshot> {
    signal?.throwIfAborted();
    const instrument = instrumentSchema.parse(value);
    const existing = this.pending.get(instrument.insCode);
    if (existing) return this.wait(existing, signal);
    const pending = this.client
      .snapshot(instrument, signal)
      .then((value) => {
        const book = orderBookSnapshotSchema.parse(value);
        if (book.instrument.insCode !== instrument.insCode)
          throw new Error('Market instrument mismatch');
        return book;
      })
      .finally(() => this.pending.delete(instrument.insCode));
    this.pending.set(instrument.insCode, pending);
    return this.wait(pending, signal);
  }

  private wait(pending: Promise<OrderBookSnapshot>, signal?: AbortSignal) {
    if (!signal) return pending;
    return new Promise<OrderBookSnapshot>((resolve, reject) => {
      const abort = () =>
        reject(
          signal.reason instanceof Error
            ? signal.reason
            : new Error('Market request aborted'),
        );
      signal.addEventListener('abort', abort, { once: true });
      void pending.then(
        (book) => {
          signal.removeEventListener('abort', abort);
          resolve(book);
        },
        (err: unknown) => {
          signal.removeEventListener('abort', abort);
          reject(
            err instanceof Error
              ? err
              : new Error('Market request failed', { cause: err }),
          );
        },
      );
      if (signal.aborted) abort();
    });
  }

  detect(book: unknown, now = Date.now()) {
    return this.detector.detect(book, now);
  }

  // Batch callers share the same in-flight registry as on-demand and scheduled requests.
  snapshots(instruments: readonly Instrument[]) {
    const unique = [
      ...new Map(
        instruments.map((instrument) => [instrument.insCode, instrument]),
      ).values(),
    ];
    return Promise.all(unique.map((instrument) => this.snapshot(instrument)));
  }

  async queue(instrument: Instrument) {
    const book = await this.snapshot(instrument);
    return this.detector.detect(book);
  }
}
