import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TsetmcClient } from '../src/market/tsetmc.client.js';
import { FixtureMarketClient } from '../src/market/fixture-client.js';
import { MarketService } from '../src/market/market-service.js';
import { QueueDetector } from '../src/market/queue-detector.js';
import type { MarketDataProvider } from '../src/market/market.types.js';
import { instrument, makeBook } from './fixtures/normalized.js';

const raw = (name: string) =>
  readFileSync(
    new URL(`./fixtures/tsetmc/${name}.json`, import.meta.url),
    'utf8',
  );
const response = (name: string) =>
  new Response(raw(name), { headers: { 'content-type': 'application/json' } });
const options = {
  baseUrl: 'https://cdn.tsetmc.com/api',
  timeoutMs: 100,
  maxRetries: 0,
};
afterEach(() => vi.useRealTimers());

describe('captured TSETMC responses (HTTP is mocked)', () => {
  it('shares server cooldown across requests and allows recovery after Retry-After', async () => {
    vi.useFakeTimers({ now: new Date('2026-10-21T06:30:00Z') });
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response('', { status: 429, headers: { 'retry-after': '60' } }),
      );
    const client = new TsetmcClient(options, fetcher);
    await expect(client.search('خودرو')).rejects.toThrow('429');
    await expect(client.search('خودرو')).rejects.toThrow('429');
    expect(fetcher).toHaveBeenCalledOnce();
    vi.setSystemTime(new Date('2026-10-21T06:31:00Z'));
    fetcher.mockResolvedValue(response('search'));
    await expect(client.search('خودرو')).resolves.toHaveLength(41);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it('cancels pending requests during shutdown without retrying', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal;
          signal?.addEventListener(
            'abort',
            () => reject(new DOMException('Request aborted', 'AbortError')),
            { once: true },
          );
        }),
    );
    const client = new TsetmcClient({ ...options, maxRetries: 2 }, fetcher);
    const pending = client.search('خودرو');
    client.close();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('times out a pending Fetch request', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal;
          signal?.addEventListener(
            'abort',
            () => reject(new DOMException('Timeout', 'TimeoutError')),
            { once: true },
          );
        }),
    );
    await expect(
      new TsetmcClient({ ...options, timeoutMs: 10 }, fetcher).search('خودرو'),
    ).rejects.toMatchObject({ name: 'TimeoutError' });
  });

  it('rejects malformed order-book quantities without fetching other endpoints', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          bestLimits: [
            {
              number: 1,
              pMeDem: 100,
              qTitMeDem: -1,
              zOrdMeDem: 1,
              pMeOf: 101,
              qTitMeOf: 2,
              zOrdMeOf: 1,
            },
          ],
        }),
      ),
    );
    await expect(
      new TsetmcClient(options, fetcher).snapshot(instrument),
    ).rejects.toThrow();
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it('validates real search results and preserves long instrument codes as strings', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response('search'));
    const instruments = await new TsetmcClient(options, fetcher).search(
      'خودرو',
    );
    expect(instruments[0]).toMatchObject({
      insCode: instrument.insCode,
      symbol: instrument.symbol,
    });
    expect(instruments.every((item) => typeof item.insCode === 'string')).toBe(
      true,
    );
    expect(fetcher.mock.calls[0]?.[0]).toContain(encodeURIComponent('خودرو'));
    expect(fetcher.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal);
  });

  it('maps observed levels and current dated limits without inventing book timestamps', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-21T06:30:00Z'));
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(response('best-1'))
      .mockResolvedValueOnce(response('closing'))
      .mockResolvedValueOnce(response('limits-date'));
    const book = await new TsetmcClient(options, fetcher).snapshot(instrument);
    expect(book.levels[0]).toEqual({
      rank: 1,
      bid: { price: 675, volume: 58605, orderCount: 1 },
      ask: { price: 679, volume: 28925201, orderCount: 23 },
    });
    expect(book.limits).toMatchObject({
      upper: 719,
      lower: 679,
      date: '20261021',
    });
    expect(book.timestamp).toBeNull();
    expect(book.completeness).toBe('unknown');
    expect(new QueueDetector().detect(book).valid).toBe(false);
    expect(fetcher.mock.calls[2]?.[0]).toContain('/20261021');
  });

  it.each([
    {
      instrumentSearch: [
        {
          insCode: Number(instrument.insCode),
          lVal18AFC: 'خودرو',
          lVal30: 'نام',
        },
      ],
    },
    { instrumentSearch: [{ insCode: '123', lVal18AFC: null, lVal30: 'نام' }] },
    { unexpected: [] },
  ])('rejects invalid external search structures', async (body) => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(JSON.stringify(body)));
    await expect(
      new TsetmcClient(options, fetcher).search('خودرو'),
    ).rejects.toThrow();
  });

  it('does not retry non-retryable HTTP errors or invalid JSON', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response('', { status: 404 }));
    await expect(
      new TsetmcClient({ ...options, maxRetries: 2 }, fetcher).search('خودرو'),
    ).rejects.toThrow('404');
    expect(fetcher).toHaveBeenCalledOnce();
    fetcher.mockReset().mockResolvedValue(new Response('{broken'));
    await expect(
      new TsetmcClient({ ...options, maxRetries: 2 }, fetcher).search('خودرو'),
    ).rejects.toThrow();
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('backs off on network errors and retries only within the configured limit', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError('network failed'))
      .mockResolvedValueOnce(response('search'));
    const instruments = await new TsetmcClient(
      { ...options, maxRetries: 1 },
      fetcher,
    ).search('خودرو');
    expect(instruments.length).toBeGreaterThan(0);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('honors rate-limit responses without aggressive retries', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response('', { status: 429, headers: { 'retry-after': '60' } }),
      );
    await expect(
      new TsetmcClient({ ...options, maxRetries: 2 }, fetcher).search('خودرو'),
    ).rejects.toThrow('429');
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('never promotes fixture data to verified market evidence', async () => {
    const fixture = new FixtureMarketClient([makeBook()]);
    expect(await fixture.search('خودرو')).toEqual([instrument]);
    expect(
      new QueueDetector().detect(await fixture.snapshot(instrument)).valid,
    ).toBe(false);
  });
});

it('shares each instrument within a batch and across concurrent users, then fetches afresh', async () => {
  const snapshot = vi
    .fn<MarketDataProvider['snapshot']>()
    .mockImplementation(() => Promise.resolve(makeBook()));
  const market = new MarketService({
    search: () => Promise.resolve([]),
    snapshot,
  });
  const [batch, concurrent] = await Promise.all([
    market.snapshots([instrument, instrument, instrument]),
    market.snapshot(instrument),
  ]);
  expect(batch).toHaveLength(1);
  expect(concurrent.instrument.insCode).toBe(instrument.insCode);
  expect(snapshot).toHaveBeenCalledOnce();
  await market.snapshot(instrument);
  expect(snapshot).toHaveBeenCalledTimes(2);
});

it('clears failed shared requests so subsequent calls can recover', async () => {
  const snapshot = vi
    .fn<MarketDataProvider['snapshot']>()
    .mockRejectedValueOnce(new Error('upstream down'))
    .mockResolvedValueOnce(makeBook());
  const market = new MarketService({
    search: () => Promise.resolve([]),
    snapshot,
  });
  await expect(market.snapshot(instrument)).rejects.toThrow('upstream down');
  await expect(market.snapshot(instrument)).resolves.toMatchObject({
    instrument,
  });
});

it('refuses market data bound to a different requested instrument', async () => {
  const snapshot = vi
    .fn<MarketDataProvider['snapshot']>()
    .mockResolvedValue(
      makeBook({ instrument: { ...instrument, insCode: '123' } }),
    );
  const market = new MarketService({
    search: () => Promise.resolve([]),
    snapshot,
  });
  await expect(market.queue(instrument)).rejects.toThrow('instrument mismatch');
});
