import { describe, expect, it } from 'vitest';
import { QueueDetector } from '../src/market/queue-detector.js';
import { makeBook } from './fixtures/normalized.js';

const detector = new QueueDetector();
const empty = { price: 0, volume: 0, orderCount: 0 };

describe('queue detection from trusted normalized books', () => {
  it('detects a buy queue only at the upper boundary', () => {
    const book = makeBook({
      levels: [
        {
          rank: 1,
          bid: { price: 110, volume: 12345, orderCount: 7 },
          ask: empty,
        },
        {
          rank: 2,
          bid: { price: 109, volume: 1000, orderCount: 5 },
          ask: empty,
        },
      ],
    });
    expect(detector.detect(book)).toMatchObject({
      valid: true,
      snapshot: { queueType: 'buy', price: 110, volume: 12345, orderCount: 7 },
    });
  });

  it('detects a sell queue only at the lower boundary', () => {
    const book = makeBook({
      levels: [
        {
          rank: 1,
          ask: { price: 90, volume: 6789, orderCount: 4 },
          bid: empty,
        },
        {
          rank: 2,
          ask: { price: 91, volume: 1000, orderCount: 5 },
          bid: empty,
        },
      ],
    });
    expect(detector.detect(book)).toMatchObject({
      valid: true,
      snapshot: { queueType: 'sell', price: 90, volume: 6789, orderCount: 4 },
    });
  });

  it('does not confuse ordinary orders or an empty complete book with a queue', () => {
    for (const book of [
      makeBook(),
      makeBook({ levels: [] }),
      makeBook({ levels: [{ rank: 1, bid: empty, ask: empty }] }),
    ]) {
      expect(detector.detect(book)).toMatchObject({
        valid: true,
        snapshot: { queueType: 'none', volume: 0, price: null, orderCount: 0 },
      });
    }
  });

  it.each([
    { verified: false },
    { completeness: 'incremental' },
    { completeness: 'unknown' },
    { timestamp: null },
    { timestamp: 1 },
    { timestamp: Date.now() + 3_600_000 },
    { tradingStatus: 'closed' },
    { tradingStatus: 'unknown' },
    { limits: null },
    { receivedAt: 1 },
    { levels: [{ rank: 2, bid: empty, ask: empty }] },
    { receivedAt: Date.now() + 3_600_000 },
    {
      levels: [
        { rank: 1, bid: { price: 110, volume: -1, orderCount: 1 }, ask: empty },
      ],
    },
    {
      levels: [
        { rank: 1, bid: { price: 110, volume: 10, orderCount: 0 }, ask: empty },
      ],
    },
    {
      levels: [
        { rank: 1, bid: { price: 110, volume: 0, orderCount: 1 }, ask: empty },
      ],
    },
    {
      levels: [
        { rank: 1, bid: { price: 111, volume: 10, orderCount: 1 }, ask: empty },
      ],
    },
    {
      levels: [
        {
          rank: 1,
          bid: { price: 100, volume: 10, orderCount: 1 },
          ask: { price: 99, volume: 2, orderCount: 1 },
        },
      ],
    },
    {
      levels: [
        {
          rank: 1,
          bid: {
            price: 100,
            volume: Number.MAX_SAFE_INTEGER + 1,
            orderCount: 1,
          },
          ask: empty,
        },
      ],
    },
    {
      levels: [
        { rank: 1, bid: empty, ask: empty },
        { rank: 1, bid: empty, ask: empty },
      ],
    },
  ])('refuses invalid, stale or ambiguous data: %o', (overrides) => {
    expect(detector.detect({ ...makeBook(), ...overrides }).valid).toBe(false);
  });

  it('checks the instrument and effective date of the limits', () => {
    const book = makeBook();
    expect(
      detector.detect({ ...book, limits: { ...book.limits, insCode: '123' } })
        .valid,
    ).toBe(false);
    expect(
      detector.detect({ ...book, limits: { ...book.limits, date: '20200101' } })
        .valid,
    ).toBe(false);
    expect(
      detector.detect({
        ...book,
        limits: { ...book.limits, upper: 80, lower: 90 },
      }).valid,
    ).toBe(false);
  });

  it('rejects duplicated prices and rank/price contradictions', () => {
    for (const price of [100, 110]) {
      const book = makeBook({
        levels: [
          {
            rank: 1,
            bid: { price: 100, volume: 10, orderCount: 1 },
            ask: empty,
          },
          { rank: 2, bid: { price, volume: 20, orderCount: 2 }, ask: empty },
        ],
      });
      expect(detector.detect(book).valid).toBe(false);
    }
  });
});
