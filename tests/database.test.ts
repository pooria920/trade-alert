import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { openDatabase } from '../src/database/client.js';
import {
  alertEvents,
  alertRules,
  alertStates,
  notificationOutbox,
  applicationState,
  queueSnapshots,
  symbols,
} from '../src/database/schema.js';

describe('SQLite migrations', () => {
  it('persists legacy and current state/outbox across reopen and migration replay', () => {
    const directory = mkdtempSync(join(tmpdir(), 'tse-db-'));
    const path = join(directory, 'nested', 'stock-alert.db');
    const insCode = '90071992547409931234';
    const timestamp = 1_700_000_000_000;
    let connection: ReturnType<typeof openDatabase> | undefined;
    try {
      connection = openDatabase(path);
      const { db, sqlite } = connection;
      expect(sqlite.pragma('journal_mode', { simple: true })).toBe('wal');
      expect(sqlite.pragma('foreign_keys', { simple: true })).toBe(1);
      db.insert(symbols)
        .values({ insCode, symbol: 'خودرو', createdAt: timestamp })
        .run();
      db.insert(alertRules)
        .values({
          id: 'rule-1',
          insCode,
          type: 'BUY_QUEUE_DROP_PERCENT',
          thresholdPercent: 40,
          armed: false,
          lastTriggeredAt: timestamp,
          createdAt: timestamp,
        })
        .run();
      db.insert(queueSnapshots)
        .values({
          insCode,
          timestamp,
          queueType: 'buy',
          volume: 100,
          price: 500,
          orderCount: 2,
        })
        .run();
      db.insert(alertEvents)
        .values({
          id: 'event-1',
          ruleId: 'rule-1',
          message: 'هشدار',
          createdAt: timestamp,
        })
        .run();
      db.insert(applicationState)
        .values({ key: 'last_update', value: '123', updatedAt: timestamp })
        .run();
      db.insert(alertStates)
        .values({
          ruleId: 'rule-1',
          revision: 1,
          sessionId: 'session-A',
          status: 'WAITING_FOR_RECOVERY',
          lastEvaluatedAt: timestamp,
          lastTriggeredAt: timestamp,
          baselineVolume: 1000,
        })
        .run();
      db.insert(notificationOutbox)
        .values({ eventId: 'event-1', nextAttemptAt: timestamp, attempts: 2 })
        .run();
      sqlite.close();

      connection = openDatabase(path);
      expect(connection.db.select().from(symbols).all()[0]?.insCode).toBe(
        insCode,
      );
      expect(connection.db.select().from(alertRules).all()[0]).toMatchObject({
        armed: false,
        lastTriggeredAt: timestamp,
      });
      expect(connection.db.select().from(queueSnapshots).all()).toHaveLength(1);
      expect(connection.db.select().from(alertEvents).all()).toHaveLength(1);
      expect(connection.db.select().from(alertStates).get()).toMatchObject({
        status: 'WAITING_FOR_RECOVERY',
        baselineVolume: 1000,
        lastEvaluatedAt: timestamp,
      });
      expect(
        connection.db.select().from(notificationOutbox).get(),
      ).toMatchObject({ status: 'PENDING', attempts: 2 });
      expect(connection.sqlite.pragma('foreign_key_check')).toEqual([]);
      expect(
        connection.db.select().from(applicationState).all()[0]?.value,
      ).toBe('123');
      expect(() =>
        connection?.db
          .insert(alertRules)
          .values({
            id: 'bad',
            insCode: 'missing',
            type: 'BUY_QUEUE_DISAPPEARED',
            createdAt: timestamp,
          })
          .run(),
      ).toThrow();
      expect(() =>
        connection?.db
          .insert(alertRules)
          .values({
            id: 'bad-threshold',
            insCode,
            type: 'BUY_QUEUE_DROP_PERCENT',
            createdAt: timestamp,
          })
          .run(),
      ).toThrow();
    } finally {
      if (connection?.sqlite.open) connection.sqlite.close();
      rmSync(directory, { recursive: true, force: true });
    }
  });
});
