import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { openDatabase } from '../src/database/client.js';
import { SnapshotRepository } from '../src/database/repositories/snapshot.repository.js';
import { SnapshotCleanup } from '../src/monitoring/snapshot-cleanup.js';
import { MarketMonitor } from '../src/market/market-monitor.js';
import {
  MarketSchedule,
  sessionsSchema,
} from '../src/market/market-schedule.js';
import { MarketService } from '../src/market/market-service.js';
import { HttpError } from '../src/market/tsetmc.client.js';
import type {
  Instrument,
  MarketDataProvider,
} from '../src/market/market.types.js';
import { UserService } from '../src/users/user-service.js';
import { createLogger } from '../src/shared/logger.js';
import { fixedNow } from './fixtures/monitoring.js';
import { instrument, makeBook } from './fixtures/normalized.js';
import { AlertService } from '../src/alerts/alert-service.js';
import { AlertEngine } from '../src/alerts/alert-engine.js';
import { alertEvents } from '../src/database/schema.js';
import { ChannelService } from '../src/alerts/channel-service.js';

vi.mock('node:timers/promises', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:timers/promises')>();
  return {
    ...actual,
    setTimeout: (
      ms: number,
      value: unknown,
      options: { signal?: AbortSignal } = {},
    ) =>
      new Promise((resolve, reject) => {
        const abort = () => {
          clearTimeout(timer);
          reject(new DOMException('Aborted', 'AbortError'));
        };
        const timer = setTimeout(() => {
          options.signal?.removeEventListener('abort', abort);
          resolve(value);
        }, ms);
        options.signal?.addEventListener('abort', abort, { once: true });
        if (options.signal?.aborted) abort();
      }),
  };
});

function bookFor(value: Instrument) {
  const book = makeBook({ instrument: value });
  return {
    ...book,
    limits: book.limits ? { ...book.limits, insCode: value.insCode } : null,
  };
}

let connection: ReturnType<typeof openDatabase>;
let users: UserService;
let repository: SnapshotRepository;
const monitors: MarketMonitor[] = [];
beforeEach(() => {
  vi.useFakeTimers({ now: fixedNow });
  connection = openDatabase(':memory:');
  users = new UserService(connection.db, ['123', '456']);
  users.register('123', '123');
  users.register('456', '456');
  repository = new SnapshotRepository(connection.db);
});
afterEach(async () => {
  for (const monitor of monitors.splice(0)) await monitor.stop();
  connection.sqlite.close();
  vi.useRealTimers();
});

function setup(
  schedule = new MarketSchedule([], [], true),
  concurrency = 2,
  timeout = 5000,
  onSnapshot: ConstructorParameters<typeof MarketMonitor>[9] = () => {},
  watched = () => users.monitoredSymbols(),
) {
  const fetch = vi
    .fn<MarketDataProvider['snapshot']>()
    .mockImplementation((value) => Promise.resolve(bookFor(value)));
  const market = new MarketService({
    search: () => Promise.resolve([]),
    snapshot: fetch,
  });
  const logger = createLogger('silent');
  const cleanup = new SnapshotCleanup(repository, 86_400_000, 900_000, logger);
  const monitor = new MarketMonitor(
    watched,
    market,
    repository,
    schedule,
    cleanup,
    {
      pollIntervalMs: 1000,
      maxConcurrentRequests: concurrency,
      requestTimeoutMs: timeout,
      maxAgeMs: 30_000,
    },
    logger,
    Date.now,
    () => 0,
    onSnapshot,
  );
  monitors.push(monitor);
  return { monitor, fetch, market, logger };
}

describe('global market collector', () => {
  it('shares collection across private memberships and independent channel rules without subscriber records', async () => {
    const channelId = '-1001234567890';
    const channels = new ChannelService(
      connection.db,
      users,
      ['123'],
      channelId,
      () =>
        Promise.resolve({ channelId, title: 'کانال آزمایشی', canPost: true }),
    );
    await channels.refresh();
    const definition = {
      type: 'BUY_QUEUE_DISAPPEARED' as const,
      config: { minimumPreviousVolume: 0 },
    };
    const first = channels.create('123', instrument, definition);
    channels.create('123', instrument, definition);
    const { monitor, fetch } = setup(
      undefined,
      2,
      5000,
      () => {},
      () => [...users.monitoredSymbols(), ...channels.monitoredSymbols()],
    );
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(1);
    users.addSymbol('456', instrument);
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(2);
    users.removeSymbol('456', instrument.insCode);
    channels.remove('123', first);
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(channels.list('123')).toHaveLength(1);
    expect(users.listSymbols('123')).toEqual([]);
  });
  it('evaluates persisted shared updates once, isolates evaluation errors and makes no per-rule fetches', async () => {
    for (const owner of ['123', '456']) {
      users.addSymbol(owner, instrument);
      new AlertService(connection.db, users).create(owner, instrument.insCode, {
        type: 'BUY_QUEUE_DISAPPEARED',
        config: { minimumPreviousVolume: 0 },
      });
    }
    const engine = new AlertEngine(
      connection.db,
      repository,
      users,
      ['123', '456'],
      30_000,
      createLogger('silent'),
    );
    const callback = vi.fn<
      NonNullable<ConstructorParameters<typeof MarketMonitor>[9]>
    >((snapshot, sessionId) => {
      expect(repository.hasAlertSnapshot(snapshot, sessionId)).toBe(true);
      engine.onSnapshot(snapshot, sessionId);
    });
    const { monitor, fetch } = setup(undefined, 2, 5000, callback);
    fetch.mockImplementation(() =>
      Promise.resolve(
        makeBook({
          levels: [
            {
              rank: 1,
              bid: { price: 110, volume: 1000, orderCount: 2 },
              ask: { price: 0, volume: 0, orderCount: 0 },
            },
          ],
        }),
      ),
    );
    await monitor.pollOnce();
    await monitor.pollOnce();
    expect(callback).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);
    fetch.mockImplementation(() => Promise.resolve(makeBook()));
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(connection.db.select().from(alertEvents).all()).toHaveLength(2);
    callback.mockImplementationOnce(() => {
      throw new Error('Broken evaluator');
    });
    await vi.advanceTimersByTimeAsync(10_000);
    await monitor.pollOnce();
    expect(monitor.getStatus().consecutiveErrors).toBe(0);
    expect(monitor.getStatus().storedSnapshots).toBe(3);
  });
  it('fetches shared memberships once, discovers changes and ignores revoked users', async () => {
    const { monitor, fetch } = setup();
    users.addSymbol('123', instrument);
    users.addSymbol('456', instrument);
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(repository.getLatestSnapshot(instrument.insCode)?.queueType).toBe(
      'none',
    );
    const other = { ...instrument, insCode: '123', symbol: 'نماد دوم' };
    users.addSymbol('456', other);
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(3);
    users.removeSymbol('123', instrument.insCode);
    users.removeSymbol('456', instrument.insCode);
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(4);
    expect(monitor.getStatus().monitoredSymbols).toBe(1);
    expect(new UserService(connection.db, ['123']).monitoredSymbols()).toEqual(
      [],
    );
  });

  it('rejects overlapping cycles and enforces bounded symbol concurrency', async () => {
    const { monitor, fetch } = setup(undefined, 2);
    for (const insCode of ['1', '2', '3'])
      users.addSymbol('123', { ...instrument, insCode });
    let active = 0;
    let maximum = 0;
    const releases: (() => void)[] = [];
    fetch.mockImplementation(
      (value) =>
        new Promise((resolve) => {
          active++;
          maximum = Math.max(maximum, active);
          releases.push(() => {
            active--;
            resolve(bookFor(value));
          });
        }),
    );
    const first = monitor.pollOnce();
    await Promise.resolve();
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(2);
    releases.splice(0).forEach((release) => release());
    for (let count = 0; count < 10; count++) await Promise.resolve();
    releases.splice(0).forEach((release) => release());
    await first;
    expect(maximum).toBe(2);
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('continues after individual API failures, backs off and records recovery', async () => {
    const { monitor, fetch, logger } = setup();
    const recovered = vi.spyOn(logger, 'info');
    users.addSymbol('123', instrument);
    users.addSymbol('456', { ...instrument, insCode: '123' });
    fetch.mockImplementation((value) =>
      value.insCode === instrument.insCode
        ? Promise.reject(new Error('Temporary outage'))
        : Promise.resolve(bookFor(value)),
    );
    await monitor.pollOnce();
    expect(repository.getLatestSnapshot('123')).not.toBeNull();
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
    expect(monitor.getStatus().consecutiveErrors).toBe(1);
    await monitor.pollOnce();
    expect(
      fetch.mock.calls.filter(
        ([value]) => value.insCode === instrument.insCode,
      ),
    ).toHaveLength(1);
    vi.setSystemTime(fixedNow + 1000);
    fetch.mockImplementation((value) => Promise.resolve(bookFor(value)));
    await monitor.pollOnce();
    expect(monitor.getStatus().consecutiveErrors).toBe(0);
    expect(
      recovered.mock.calls.some((call) => call[1] === 'Market data recovered'),
    ).toBe(true);
    const copy = monitor.getStatus();
    copy.symbols[0]!.reason = 'changed externally';
    expect(monitor.getStatus().symbols[0]?.reason).toBeNull();
  });

  it('distinguishes successful API fetches from invalid/stale data and never stores failures', async () => {
    const { monitor, fetch } = setup();
    users.addSymbol('123', instrument);
    fetch.mockResolvedValue(
      makeBook({ verified: false, completeness: 'unknown', timestamp: null }),
    );
    await monitor.pollOnce();
    expect(monitor.getStatus()).toMatchObject({
      lastSuccessfulFetch: fixedNow,
      lastVerifiedSnapshot: null,
      consecutiveErrors: 1,
    });
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
    vi.setSystemTime(fixedNow + 1000);
    fetch.mockResolvedValue(makeBook({ timestamp: fixedNow - 60_000 }));
    await monitor.pollOnce();
    expect(monitor.getStatus().symbols[0]?.lastResult).toBe('stale-data');
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
  });

  it('persists buy and sell boundary states and does not duplicate reliable source updates', async () => {
    const { monitor, fetch } = setup();
    users.addSymbol('123', instrument);
    const empty = { price: 0, volume: 0, orderCount: 0 };
    const buy = makeBook({
      levels: [
        {
          rank: 1,
          bid: { price: 110, volume: 1000, orderCount: 2 },
          ask: empty,
        },
      ],
    });
    fetch.mockResolvedValue(buy);
    await monitor.pollOnce();
    await monitor.pollOnce();
    expect(
      repository.getSnapshotsBetween(instrument.insCode, 0, fixedNow),
    ).toHaveLength(1);
    vi.setSystemTime(fixedNow + 1000);
    fetch.mockResolvedValue(
      makeBook({
        levels: [
          {
            rank: 1,
            bid: empty,
            ask: { price: 90, volume: 500, orderCount: 1 },
          },
        ],
      }),
    );
    await monitor.pollOnce();
    expect(repository.getLatestSnapshot(instrument.insCode)).toMatchObject({
      queueType: 'sell',
      volume: 500,
      timestamp: fixedNow + 1000,
      collectedAt: fixedNow + 1000,
    });
    expect(monitor.getStatus().storedSnapshots).toBe(2);
  });

  it('starts once, pauses/resumes on configured sessions, and cancels its delay on stop', async () => {
    const sessions = sessionsSchema.parse([
      { days: [3], start: '10:01', end: '10:02' },
    ]);
    const { monitor, fetch } = setup(new MarketSchedule(sessions));
    users.addSymbol('123', instrument);
    monitor.start();
    monitor.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).not.toHaveBeenCalled();
    vi.setSystemTime(fixedNow + 60_000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).toHaveBeenCalledTimes(1);
    vi.setSystemTime(fixedNow + 120_000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).toHaveBeenCalledTimes(1);
    await monitor.stop();
    expect(monitor.getStatus().isRunning).toBe(false);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('cancels active collection on stop without storing late responses', async () => {
    const { monitor, fetch } = setup();
    users.addSymbol('123', instrument);
    fetch.mockImplementation(
      (_value, signal) =>
        new Promise((_resolve, reject) =>
          signal?.addEventListener(
            'abort',
            () => reject(new Error('aborted')),
            { once: true },
          ),
        ),
    );
    monitor.start();
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledOnce();
    await monitor.stop();
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
    expect(monitor.getStatus().consecutiveErrors).toBe(0);
  });

  it('also joins an active one-off cycle when stopped', async () => {
    const { monitor, fetch } = setup();
    users.addSymbol('123', instrument);
    fetch.mockImplementation(
      (_value, signal) =>
        new Promise((_resolve, reject) =>
          signal?.addEventListener(
            'abort',
            () => reject(new Error('cancelled')),
            { once: true },
          ),
        ),
    );
    const cycle = monitor.pollOnce();
    await monitor.stop();
    await cycle;
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
  });

  it('bounds a symbol collection even if a provider ignores cancellation', async () => {
    const { monitor, fetch } = setup(undefined, 1, 10);
    users.addSymbol('123', instrument);
    fetch.mockImplementation(() => new Promise(() => {}));
    // Native AbortSignal.timeout is independent of fake Date/timer scheduling.
    await monitor.pollOnce();
    expect(monitor.getStatus().consecutiveErrors).toBe(1);
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
  });

  it('respects a global Retry-After before starting other queued symbols', async () => {
    const { monitor, fetch } = setup(undefined, 1);
    users.addSymbol('123', { ...instrument, insCode: '1' });
    users.addSymbol('123', { ...instrument, insCode: '2' });
    fetch.mockRejectedValue(new HttpError(429, 60000));
    await monitor.pollOnce();
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledOnce();
    expect(monitor.getStatus().rateLimitedUntil).toBe(fixedNow + 60000);
    vi.setSystemTime(fixedNow + 60000);
    fetch.mockImplementation((value) => Promise.resolve(bookFor(value)));
    await monitor.pollOnce();
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('does not persist an update if membership is removed during its request', async () => {
    const { monitor, fetch } = setup();
    users.addSymbol('123', instrument);
    fetch.mockImplementation(() => {
      users.removeSymbol('123', instrument.insCode);
      return Promise.resolve(makeBook());
    });
    await monitor.pollOnce();
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
  });
});
