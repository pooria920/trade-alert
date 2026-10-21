import { setTimeout as sleep } from 'node:timers/promises';
import { z } from 'zod';
import {
  bestLimitsResponseSchema,
  closingResponseSchema,
  limitsResponseSchema,
  searchResponseSchema,
} from './tsetmc.schemas.js';
import {
  dailyPriceLimitsSchema,
  instrumentSchema,
  normalizePersian,
  tehranDate,
  type Instrument,
  type MarketDataProvider,
  type OrderBookSnapshot,
} from './market.types.js';

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly retryAfter: number | null,
  ) {
    super(`TSETMC HTTP ${status}`);
  }
}

export interface TsetmcOptions {
  baseUrl: string;
  timeoutMs: number;
  maxRetries: number;
}

export class TsetmcClient implements MarketDataProvider {
  private readonly shutdown = new AbortController();
  private cooldown: { until: number; error: HttpError } | null = null;
  constructor(
    private readonly options: TsetmcOptions,
    private readonly fetchImpl: typeof fetch = globalThis.fetch,
  ) {}

  close() {
    this.shutdown.abort();
  }

  private async request<T>(
    path: string,
    schema: z.ZodType<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      if (this.cooldown && Date.now() < this.cooldown.until)
        throw this.cooldown.error;
      try {
        const response = await this.fetchImpl(
          `${this.options.baseUrl.replace(/\/$/, '')}/${path}`,
          {
            signal: AbortSignal.any([
              this.shutdown.signal,
              AbortSignal.timeout(this.options.timeoutMs),
              ...(signal ? [signal] : []),
            ]),
            headers: { Accept: 'application/json' },
            redirect: 'error',
          },
        );
        if (!response.ok) {
          const header = response.headers.get('retry-after');
          const seconds = header === null ? NaN : Number(header);
          const retryAfter =
            header === null
              ? null
              : Number.isFinite(seconds)
                ? Math.max(0, seconds * 1000)
                : Math.max(0, Date.parse(header) - Date.now());
          await response.body?.cancel();
          const error = new HttpError(
            response.status,
            Number.isFinite(retryAfter) ? retryAfter : null,
          );
          if (error.status === 429 || error.retryAfter !== null) {
            const until = Date.now() + (error.retryAfter ?? 500);
            if (!this.cooldown || until > this.cooldown.until)
              this.cooldown = { until, error };
          }
          throw error;
        }
        const body: unknown = await response.json();
        return schema.parse(body);
      } catch (error) {
        if (this.shutdown.signal.aborted || signal?.aborted) throw error;
        const retryable =
          error instanceof HttpError
            ? [429, 500, 502, 503, 504].includes(error.status)
            : error instanceof TypeError ||
              (error instanceof Error &&
                ['TimeoutError', 'AbortError'].includes(error.name));
        if (!retryable || attempt >= this.options.maxRetries) throw error;
        const backoff = Math.max(
          500 * 2 ** attempt * (1 + Math.random() * 0.25),
          error instanceof HttpError ? (error.retryAfter ?? 0) : 0,
        );
        if (backoff > 30_000) throw error;
        try {
          await sleep(backoff, undefined, {
            signal: signal
              ? AbortSignal.any([signal, this.shutdown.signal])
              : this.shutdown.signal,
          });
        } catch (waitError) {
          // Keep server cooldown information if a caller deadline interrupts Retry-After.
          if (error instanceof HttpError) throw error;
          throw waitError;
        }
      }
    }
  }

  async search(symbol: string): Promise<Instrument[]> {
    const query = z.string().trim().min(1).max(80).parse(symbol);
    const response = await this.request(
      `Instrument/GetInstrumentSearch/${encodeURIComponent(normalizePersian(query))}`,
      searchResponseSchema,
    );
    return response.instrumentSearch.map((row) =>
      instrumentSchema.parse({
        insCode: row.insCode,
        symbol: normalizePersian(row.lVal18AFC),
        name: normalizePersian(row.lVal30),
      }),
    );
  }

  async snapshot(
    value: Instrument,
    signal?: AbortSignal,
  ): Promise<OrderBookSnapshot> {
    const instrument = instrumentSchema.parse(value);
    const date = tehranDate(Date.now());
    const book = await this.request(
      `BestLimits/${instrument.insCode}`,
      bestLimitsResponseSchema,
      signal,
    );
    const closing = await this.request(
      `ClosingPrice/GetClosingPriceInfo/${instrument.insCode}`,
      closingResponseSchema,
      signal,
    );
    const thresholds = await this.request(
      `MarketData/GetStaticThreshold/${instrument.insCode}/${date}`,
      limitsResponseSchema,
      signal,
    );
    const limit = thresholds.staticThreshold
      .filter((row) => String(row.dEven) === date)
      .sort((a, b) => b.hEven - a.hEven)[0];
    const limits = limit
      ? dailyPriceLimitsSchema.parse({
          insCode: instrument.insCode,
          date,
          upper: limit.psGelStaMax,
          lower: limit.psGelStaMin,
        })
      : null;
    const state = closing.closingPriceInfo.instrumentState?.cEtaval.trim();
    return {
      instrument,
      // BestLimits has no source timestamp; a closing trade timestamp is not a book timestamp.
      timestamp: null,
      receivedAt: Date.now(),
      completeness: 'unknown',
      verified: false,
      tradingStatus:
        String(closing.closingPriceInfo.dEven) !== date
          ? 'unknown'
          : state === 'A'
            ? 'open'
            : 'unknown',
      limits,
      levels: book.bestLimits.map((row) => ({
        rank: row.number,
        bid: {
          price: row.pMeDem,
          volume: row.qTitMeDem,
          orderCount: row.zOrdMeDem,
        },
        ask: {
          price: row.pMeOf,
          volume: row.qTitMeOf,
          orderCount: row.zOrdMeOf,
        },
      })),
    };
  }
}
