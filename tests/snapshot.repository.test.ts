import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { openDatabase } from '../src/database/client.js';
import { SnapshotRepository } from '../src/database/repositories/snapshot.repository.js';
import {
  alertRules,
  queueSnapshots,
  symbols,
  users,
  watchlists,
} from '../src/database/schema.js';
import { SnapshotCleanup } from '../src/monitoring/snapshot-cleanup.js';
import { createLogger } from '../src/shared/logger.js';
import { instrument } from './fixtures/normalized.js';
import { fixedNow } from './fixtures/monitoring.js';

let connection: ReturnType<typeof openDatabase>;
let repository: SnapshotRepository;
const snapshot = (timestamp = fixedNow) => ({
  insCode: instrument.insCode,
  symbol: instrument.symbol,
  timestamp,
  queueType: 'none' as const,
  volume: 0,
  price: null,
  orderCount: 0,
});
beforeEach(() => {
  vi.useFakeTimers({ now: fixedNow });
  connection = openDatabase(':memory:');
  repository = new SnapshotRepository(connection.db, 20_000);
  connection.db
    .insert(symbols)
    .values({
      insCode: instrument.insCode,
      symbol: instrument.symbol,
      createdAt: fixedNow,
    })
    .run();
});
afterEach(() => {
  connection.sqlite.close();
  vi.useRealTimers();
});

describe('snapshot persistence and bounded history', () => {
  it('preserves verified no-queue zero states, source/collection times and deduplicates', () => {
    expect(repository.save(snapshot(), fixedNow + 100)).toBe(true);
    expect(repository.save(snapshot(), fixedNow + 200)).toBe(false);
    expect(repository.getLatestSnapshot(instrument.insCode)).toEqual({
      ...snapshot(),
      collectedAt: fixedNow + 100,
    });
    expect(() =>
      repository.save(
        {
          ...snapshot(),
          queueType: 'buy',
          volume: 10,
          price: 110,
          orderCount: 2,
        },
        fixedNow + 200,
      ),
    ).toThrow('Conflicting');
    expect(() =>
      repository.save({ ...snapshot(), volume: -1 }, fixedNow),
    ).toThrow();
    expect(() => repository.save(snapshot(), fixedNow - 1)).toThrow('precedes');
  });

  it('supports one/five-minute baselines without substituting old or future updates', () => {
    for (const age of [0, 70_000, 320_000, 400_000])
      repository.save(snapshot(fixedNow - age), fixedNow);
    expect(
      repository.getSnapshotAtOrBefore(instrument.insCode, fixedNow - 60_000)
        ?.timestamp,
    ).toBe(fixedNow - 70_000);
    expect(
      repository.getSnapshotAtOrBefore(instrument.insCode, fixedNow - 300_000)
        ?.timestamp,
    ).toBe(fixedNow - 320_000);
    expect(
      repository.getSnapshotAtOrBefore(instrument.insCode, fixedNow - 90_000),
    ).toBeNull();
    expect(
      repository.getSnapshotAtOrBefore(instrument.insCode, fixedNow - 330_000),
    ).toBeNull();
    expect(repository.getSnapshotAtOrBefore('123', fixedNow)).toBeNull();
    expect(
      repository
        .getSnapshotsBetween(
          instrument.insCode,
          fixedNow - 320_000,
          fixedNow - 70_000,
        )
        .map((row) => row.timestamp),
    ).toEqual([fixedNow - 320_000, fixedNow - 70_000]);
    expect(() =>
      repository.getSnapshotsBetween(
        instrument.insCode,
        fixedNow,
        fixedNow - 1,
      ),
    ).toThrow('range');
  });

  it('preserves legacy rows without fabricating their provenance or using them as baselines', () => {
    connection.db.insert(queueSnapshots).values(snapshot()).run();
    expect(repository.getLatestSnapshot(instrument.insCode)).toBeNull();
    expect(
      repository.getSnapshotAtOrBefore(instrument.insCode, fixedNow),
    ).toBeNull();
    expect(
      repository.getSnapshotsBetween(instrument.insCode, 0, fixedNow),
    ).toEqual([]);
    expect(() => repository.save(snapshot(), fixedNow)).toThrow('legacy');
    expect(connection.db.select().from(queueSnapshots).all()).toHaveLength(1);
  });
});

it('cleans only expired snapshots periodically and preserves persistent configuration', async () => {
  const cutoff = fixedNow - 86_400_000;
  for (const at of [cutoff - 1, cutoff, fixedNow])
    repository.save(snapshot(at), fixedNow);
  connection.db
    .insert(users)
    .values({
      telegramUserId: '123',
      privateChatId: '123',
      createdAt: fixedNow,
      updatedAt: fixedNow,
    })
    .run();
  connection.db
    .insert(watchlists)
    .values({ userId: '123', insCode: instrument.insCode, createdAt: fixedNow })
    .run();
  connection.db
    .insert(alertRules)
    .values({
      id: 'keep',
      ownerId: '123',
      insCode: instrument.insCode,
      type: 'BUY_QUEUE_DISAPPEARED',
      createdAt: fixedNow,
    })
    .run();
  const cleanup = new SnapshotCleanup(
    repository,
    86_400_000,
    900_000,
    createLogger('silent'),
  );
  const spy = vi.spyOn(repository, 'deleteExpired');
  const controller = new AbortController();
  cleanup.runIfDue(fixedNow, controller.signal);
  await cleanup.wait();
  expect(
    repository.getSnapshotsBetween(instrument.insCode, 0, fixedNow),
  ).toHaveLength(2);
  cleanup.runIfDue(fixedNow + 1000, controller.signal);
  await cleanup.wait();
  expect(spy).toHaveBeenCalledTimes(1);
  expect(connection.db.select().from(users).all()).toHaveLength(1);
  expect(connection.db.select().from(watchlists).all()).toHaveLength(1);
  expect(connection.db.select().from(alertRules).all()).toHaveLength(1);
  expect(connection.db.select().from(symbols).all()).toHaveLength(1);
});

it('yields between bounded cleanup batches instead of draining history synchronously', async () => {
  const cutoff = fixedNow - 86_400_000;
  const insert = connection.sqlite.prepare(
    "INSERT INTO queue_snapshots (ins_code,symbol,timestamp,collected_at,queue_type,volume,price,order_count) VALUES (?,?,?,?,'none',0,NULL,0)",
  );
  connection.sqlite.transaction(() => {
    for (let i = 1; i <= 1001; i++)
      insert.run(instrument.insCode, instrument.symbol, cutoff - i, fixedNow);
  })();
  const cleanup = new SnapshotCleanup(
    repository,
    86_400_000,
    900_000,
    createLogger('silent'),
  );
  const spy = vi.spyOn(repository, 'deleteExpired');
  cleanup.runIfDue(fixedNow, new AbortController().signal);
  expect(spy).toHaveBeenCalledTimes(1);
  expect(connection.db.select().from(queueSnapshots).all()).toHaveLength(1);
  await cleanup.wait();
  expect(spy).toHaveBeenCalledTimes(2);
  expect(connection.db.select().from(queueSnapshots).all()).toEqual([]);
});
