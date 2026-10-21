import {
  tehranDate,
  type OrderBookSnapshot,
} from '../../src/market/market.types.js';

// Synthetic normalized domain data for algorithm tests; not a captured API response.
export const instrument = {
  insCode: '65883838195688438',
  symbol: 'خودرو',
  name: 'ایران خودرو',
};
export function makeBook(
  overrides: Partial<OrderBookSnapshot> = {},
): OrderBookSnapshot {
  const now = Date.now();
  return {
    instrument,
    timestamp: now,
    receivedAt: now,
    tradingStatus: 'open',
    completeness: 'complete',
    verified: true,
    limits: {
      insCode: instrument.insCode,
      date: tehranDate(now),
      lower: 90,
      upper: 110,
    },
    levels: [
      {
        rank: 1,
        bid: { price: 100, volume: 50, orderCount: 2 },
        ask: { price: 101, volume: 60, orderCount: 3 },
      },
    ],
    ...overrides,
  };
}
