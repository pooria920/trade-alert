import { z } from 'zod';

export const insCodeSchema = z.string().regex(/^[1-9]\d{0,19}$/);
export const instrumentSchema = z.object({
  insCode: insCodeSchema,
  symbol: z.string().trim().min(1).max(80),
  name: z.string().trim().min(1).max(200),
});
export type Instrument = z.infer<typeof instrumentSchema>;

const quantity = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER);
export const orderBookLevelSchema = z.object({
  rank: z.number().int().positive().max(100),
  bid: z.object({
    price: z.number().nonnegative(),
    volume: quantity,
    orderCount: quantity,
  }),
  ask: z.object({
    price: z.number().nonnegative(),
    volume: quantity,
    orderCount: quantity,
  }),
});
export type OrderBookLevel = z.infer<typeof orderBookLevelSchema>;

export const dailyPriceLimitsSchema = z
  .object({
    insCode: insCodeSchema,
    date: z.string().regex(/^\d{8}$/),
    upper: z.number().positive(),
    lower: z.number().positive(),
  })
  .refine((limits) => limits.upper > limits.lower);
export type DailyPriceLimits = z.infer<typeof dailyPriceLimitsSchema>;

export const orderBookSnapshotSchema = z.object({
  instrument: instrumentSchema,
  timestamp: z.number().int().nonnegative().nullable(),
  receivedAt: z.number().int().nonnegative(),
  tradingStatus: z.enum(['open', 'closed', 'unknown']),
  completeness: z.enum(['complete', 'incremental', 'unknown']),
  verified: z.boolean(),
  levels: z.array(orderBookLevelSchema).max(100),
  limits: dailyPriceLimitsSchema.nullable(),
});
export type OrderBookSnapshot = z.infer<typeof orderBookSnapshotSchema>;

export const queueSnapshotSchema = z
  .object({
    insCode: insCodeSchema,
    symbol: instrumentSchema.shape.symbol,
    timestamp: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    queueType: z.enum(['buy', 'sell', 'none']),
    volume: quantity,
    price: z.number().positive().nullable(),
    orderCount: quantity,
  })
  .refine((snapshot) =>
    snapshot.queueType === 'none'
      ? snapshot.volume === 0 &&
        snapshot.orderCount === 0 &&
        snapshot.price === null
      : snapshot.volume > 0 &&
        snapshot.orderCount > 0 &&
        snapshot.orderCount <= snapshot.volume &&
        snapshot.price !== null,
  );
export type QueueSnapshot = z.infer<typeof queueSnapshotSchema>;

export interface MarketDataProvider {
  search(symbol: string): Promise<Instrument[]>;
  snapshot(
    instrument: Instrument,
    signal?: AbortSignal,
  ): Promise<OrderBookSnapshot>;
}

export function tehranDate(timestamp: number): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Tehran',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(timestamp);
  return ['year', 'month', 'day']
    .map((type) => parts.find((part) => part.type === type)?.value)
    .join('');
}

export function normalizePersian(value: string): string {
  return value.replaceAll('ي', 'ی').replaceAll('ك', 'ک').trim();
}
