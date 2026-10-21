import {
  orderBookSnapshotSchema,
  tehranDate,
  type QueueSnapshot,
} from './market.types.js';

export type QueueDetection =
  { valid: true; snapshot: QueueSnapshot } | { valid: false; reason: string };

export class QueueDetector {
  constructor(private readonly maxAgeMs = 30_000) {}

  detect(input: unknown, now = Date.now()): QueueDetection {
    const parsed = orderBookSnapshotSchema.safeParse(input);
    if (!parsed.success) return { valid: false, reason: 'invalid-data' };
    const book = parsed.data;
    if (!book.verified || book.completeness !== 'complete')
      return { valid: false, reason: 'unverified-book' };
    if (
      book.timestamp === null ||
      book.timestamp > now ||
      now - book.timestamp > this.maxAgeMs ||
      book.receivedAt > now ||
      book.receivedAt < book.timestamp ||
      now - book.receivedAt > this.maxAgeMs
    )
      return { valid: false, reason: 'stale-data' };
    if (book.tradingStatus !== 'open')
      return { valid: false, reason: 'trading-state' };
    const limits = book.limits;
    if (
      !limits ||
      limits.insCode !== book.instrument.insCode ||
      limits.date !== tehranDate(book.timestamp) ||
      limits.date !== tehranDate(now)
    )
      return { valid: false, reason: 'invalid-limits' };
    const ranks = new Set<number>();
    let bestBid = 0;
    let bestAsk = Infinity;
    let previousBid = Infinity;
    let previousAsk = 0;
    for (const level of book.levels.toSorted((a, b) => a.rank - b.rank)) {
      if (ranks.has(level.rank))
        return { valid: false, reason: 'duplicate-level' };
      ranks.add(level.rank);
      for (const side of [level.bid, level.ask]) {
        const empty = side.volume === 0 && side.orderCount === 0;
        if (
          !empty &&
          (side.volume <= 0 ||
            side.orderCount <= 0 ||
            side.orderCount > side.volume ||
            side.price < limits.lower ||
            side.price > limits.upper)
        )
          return { valid: false, reason: 'inconsistent-order' };
      }
      if (
        (level.bid.volume && level.bid.price >= previousBid) ||
        (level.ask.volume && level.ask.price <= previousAsk)
      )
        return { valid: false, reason: 'inconsistent-level-order' };
      previousBid = level.bid.volume ? level.bid.price : 0;
      previousAsk = level.ask.volume ? level.ask.price : Infinity;
      if (level.bid.volume) bestBid = Math.max(bestBid, level.bid.price);
      if (level.ask.volume) bestAsk = Math.min(bestAsk, level.ask.price);
    }
    if (ranks.size && Math.max(...ranks) !== ranks.size)
      return { valid: false, reason: 'missing-level' };
    if (bestBid >= bestAsk) return { valid: false, reason: 'crossed-book' };
    const queueType =
      bestBid === limits.upper
        ? 'buy'
        : bestAsk === limits.lower
          ? 'sell'
          : 'none';
    let volume = 0;
    let orderCount = 0;
    const price =
      queueType === 'buy'
        ? limits.upper
        : queueType === 'sell'
          ? limits.lower
          : null;
    if (queueType !== 'none') {
      for (const level of book.levels) {
        const side = queueType === 'buy' ? level.bid : level.ask;
        if (side.price === price && side.volume > 0) {
          volume += side.volume;
          orderCount += side.orderCount;
        }
      }
    }
    if (!Number.isSafeInteger(volume) || !Number.isSafeInteger(orderCount))
      return { valid: false, reason: 'unsafe-quantity' };
    return {
      valid: true,
      snapshot: {
        insCode: book.instrument.insCode,
        symbol: book.instrument.symbol,
        timestamp: book.timestamp,
        queueType,
        volume,
        price,
        orderCount,
      },
    };
  }
}
