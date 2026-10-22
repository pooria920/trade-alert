import { afterEach, describe, expect, it, vi } from 'vitest';
import { eq } from 'drizzle-orm';
import { openDatabase } from '../src/database/client.js';
import {
  alertEvents,
  alertRules,
  alertStates,
  notificationOutbox,
} from '../src/database/schema.js';
import { SnapshotRepository } from '../src/database/repositories/snapshot.repository.js';
import { UserService } from '../src/users/user-service.js';
import { AlertService } from '../src/alerts/alert-service.js';
import { AlertEngine } from '../src/alerts/alert-engine.js';
import { evaluate } from '../src/alerts/evaluators.js';
import {
  ruleDefinitionSchema,
  type RuleDefinition,
} from '../src/alerts/alert.types.js';
import type { QueueSnapshot } from '../src/market/market.types.js';
import { createLogger } from '../src/shared/logger.js';
import {
  NotificationDispatcher,
  type SendNotification,
} from '../src/notifications/notification-dispatcher.js';
import { formatNotification } from '../src/notifications/notifier.js';
import { instrument } from './fixtures/normalized.js';

const start = Date.parse('2026-10-21T06:30:00Z');
const drop: RuleDefinition = {
  type: 'BUY_QUEUE_DROP_PERCENT',
  config: { thresholdPercent: 30, windowMinutes: 1, minimumBaselineVolume: 0 },
};
const gone: RuleDefinition = {
  type: 'BUY_QUEUE_DISAPPEARED',
  config: { minimumPreviousVolume: 0 },
};
const snapshot = (
  seconds: number,
  volume = 1000,
  queueType: QueueSnapshot['queueType'] = 'buy',
): QueueSnapshot => ({
  insCode: instrument.insCode,
  symbol: instrument.symbol,
  timestamp: start + seconds * 1000,
  queueType,
  volume: queueType === 'none' ? 0 : volume,
  orderCount: queueType === 'none' ? 0 : 1,
  price: queueType === 'none' ? null : 110,
});
const connections: ReturnType<typeof openDatabase>[] = [];
afterEach(() => {
  for (const connection of connections.splice(0)) connection.sqlite.close();
  vi.useRealTimers();
});
function setup() {
  const connection = openDatabase(':memory:');
  connections.push(connection);
  const { db } = connection;
  const users = new UserService(db, ['123', '456']);
  for (const id of ['123', '456']) {
    users.register(id, id);
    users.addSymbol(id, instrument);
  }
  let now = start;
  const clock = () => now;
  const service = new AlertService(db, users, 300, 10, clock);
  const repository = new SnapshotRepository(db, 20_000);
  const logger = createLogger('silent');
  let engine = new AlertEngine(
    db,
    repository,
    users,
    ['123', '456'],
    30_000,
    logger,
    clock,
  );
  const update = (
    seconds: number,
    volume = 1000,
    type: QueueSnapshot['queueType'] = 'buy',
    session = 'session-A',
  ) => {
    now = start + seconds * 1000;
    const value = snapshot(seconds, volume, type);
    repository.save(value, now, session);
    engine.onSnapshot(value, session);
    return value;
  };
  const dispatcher = (send: SendNotification, maxRetries = 5) =>
    new NotificationDispatcher(
      db,
      users,
      send,
      {
        maxRetries,
        retryBaseMs: 2000,
        maxAgeMs: 900_000,
        marketMaxAgeMs: 30_000,
      },
      logger,
      clock,
    );
  const trigger = () => {
    const id = service.create('123', instrument.insCode, drop);
    update(0);
    update(60, 700);
    return id;
  };
  return {
    ...connection,
    users,
    service,
    repository,
    update,
    dispatcher,
    trigger,
    clock,
    engine: () => engine,
    setTime: (seconds: number) => {
      now = start + seconds * 1000;
    },
    restart: () => {
      engine = new AlertEngine(
        db,
        repository,
        users,
        ['123', '456'],
        30_000,
        logger,
        clock,
      );
    },
  };
}

describe('typed evaluators', () => {
  it.each(['buy', 'sell'] as const)(
    'evaluates %s drop, including exact decimal boundaries',
    (side) => {
      const definition = {
        type:
          side === 'buy'
            ? ('BUY_QUEUE_DROP_PERCENT' as const)
            : ('SELL_QUEUE_DROP_PERCENT' as const),
        config: {
          thresholdPercent: 30,
          windowMinutes: 1 as const,
          minimumBaselineVolume: 0,
        },
      };
      const baseline = snapshot(0, 1000, side);
      expect(
        evaluate(definition, {
          baseline,
          previous: null,
          current: snapshot(60, 700, side),
        }),
      ).toMatchObject({ status: 'TRIGGER', actualPercent: 30 });
      expect(
        evaluate(definition, {
          baseline,
          previous: null,
          current: snapshot(60, 701, side),
        }),
      ).toEqual({ status: 'CLEAR' });
      definition.config.thresholdPercent = 0.1;
      expect(
        evaluate(definition, {
          baseline,
          previous: null,
          current: snapshot(60, 999, side),
        }).status,
      ).toBe('TRIGGER');
    },
  );
  it.each(['buy', 'sell'] as const)(
    'requires a confirmed %s queue before disappearance',
    (side) => {
      const definition: RuleDefinition = {
        type:
          side === 'buy' ? 'BUY_QUEUE_DISAPPEARED' : 'SELL_QUEUE_DISAPPEARED',
        config: { minimumPreviousVolume: 100 },
      };
      const context = {
        baseline: null,
        previous: snapshot(0, 100, side),
        current: snapshot(10, 0, 'none'),
      };
      expect(evaluate(definition, context)).toMatchObject({
        status: 'TRIGGER',
        previousVolume: 100,
        currentVolume: 0,
      });
      expect(evaluate(definition, { ...context, previous: null }).status).toBe(
        'INSUFFICIENT_DATA',
      );
      expect(
        evaluate(definition, { ...context, previous: snapshot(0, 99, side) })
          .status,
      ).toBe('INSUFFICIENT_DATA');
      expect(
        evaluate(definition, {
          ...context,
          current: snapshot(10, 100, side === 'buy' ? 'sell' : 'buy'),
        }).status,
      ).toBe('INSUFFICIENT_DATA');
    },
  );
  it('rejects missing, zero, insufficient and incompatible drop baselines without division', () => {
    for (const baseline of [
      null,
      snapshot(0, 0),
      snapshot(0, 1000, 'sell'),
      snapshot(0, 0, 'none'),
    ])
      expect(
        evaluate(drop, { baseline, previous: null, current: snapshot(60, 1) })
          .status,
      ).toBe('INSUFFICIENT_DATA');
    expect(
      evaluate(drop, {
        baseline: snapshot(0),
        previous: null,
        current: snapshot(60, 100, 'sell'),
      }).status,
    ).toBe('INSUFFICIENT_DATA');
    expect(
      evaluate(
        {
          type: 'BUY_QUEUE_DROP_PERCENT',
          config: {
            thresholdPercent: 100,
            windowMinutes: 1,
            minimumBaselineVolume: 0,
          },
        },
        {
          baseline: snapshot(0),
          previous: null,
          current: snapshot(60, 0, 'none'),
        },
      ),
    ).toMatchObject({
      status: 'TRIGGER',
      actualPercent: 100,
      currentVolume: 0,
    });
  });
  it.each([
    { ...drop, config: { ...drop.config, thresholdPercent: 0 } },
    { ...drop, config: { ...drop.config, thresholdPercent: 101 } },
    { ...drop, config: { ...drop.config, thresholdPercent: 0.0000001 } },
    { ...drop, config: { ...drop.config, windowMinutes: 2 } },
    { ...drop, config: { ...drop.config, minimumBaselineVolume: -1 } },
    { ...gone, config: { minimumPreviousVolume: -1 } },
  ])('rejects invalid config before storage: %j', (value) => {
    expect(ruleDefinitionSchema.safeParse(value).success).toBe(false);
  });
});

describe('persistent engine and rule ownership', () => {
  it('fires a 100 percent drop only from a persisted detector-confirmed none', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, {
      type: 'BUY_QUEUE_DROP_PERCENT',
      config: {
        thresholdPercent: 100,
        windowMinutes: 1,
        minimumBaselineVolume: 0,
      },
    });
    s.update(0);
    s.update(60, 0, 'none');
    expect(s.service.history('123')).toHaveLength(1);
    expect(
      JSON.parse(s.service.history('123')[0]?.payload ?? 'null'),
    ).toMatchObject({ actualPercent: 100, currentVolume: 0 });
  });
  it('rearms disappearance on confirmed recovery and requires full recovery for small drop thresholds', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, gone, 0);
    s.update(0);
    s.update(10, 0, 'none');
    s.update(20, 0, 'none');
    s.update(30, 1);
    s.update(40, 0, 'none');
    expect(s.db.select().from(alertEvents).all()).toHaveLength(2);
    const t = setup();
    t.service.create(
      '123',
      instrument.insCode,
      {
        type: 'BUY_QUEUE_DROP_PERCENT',
        config: {
          thresholdPercent: 3,
          windowMinutes: 1,
          minimumBaselineVolume: 0,
        },
      },
      0,
    );
    t.update(0);
    t.update(60, 970);
    t.update(70, 999);
    expect(t.db.select().from(alertStates).get()?.status).toBe(
      'WAITING_FOR_RECOVERY',
    );
    t.update(80, 1000);
    expect(t.db.select().from(alertStates).get()?.status).toBe('ARMED');
  });
  it('uses bounded one/five minute history and primes new states', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, drop);
    s.service.create('456', instrument.insCode, {
      type: 'BUY_QUEUE_DROP_PERCENT',
      config: {
        thresholdPercent: 30,
        windowMinutes: 5,
        minimumBaselineVolume: 0,
      },
    });
    s.update(0);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(0);
    s.update(60, 700);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(1);
    s.update(300, 700);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(2);
    expect(s.db.select().from(notificationOutbox).all()).toHaveLength(2);
  });
  it('does not trigger without history or beyond historical tolerance', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, drop);
    s.update(0);
    s.update(30, 100);
    s.update(120, 50);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(0);
  });
  it('fires disappearance only from a recent same-session confirmed queue', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, gone);
    s.update(0);
    s.update(10, 0, 'none');
    s.update(20, 0, 'none');
    expect(s.db.select().from(alertEvents).all()).toHaveLength(1);
    expect(s.db.select().from(alertStates).get()?.status).toBe(
      'WAITING_FOR_RECOVERY',
    );
  });
  it('ignores stale, future, invalid, nonpersisted, duplicate and late updates', () => {
    const s = setup();
    const id = s.trigger();
    s.engine().onSnapshot(snapshot(60, 700), 'session-A');
    s.update(50, 200); // accepted into history but never rolls state backwards
    expect(s.db.select().from(alertStates).get()?.lastEvaluatedAt).toBe(
      start + 60_000,
    );
    s.setTime(120);
    for (const value of [
      snapshot(60, 700),
      snapshot(121),
      { ...snapshot(120), volume: -1 },
      snapshot(120, 1),
    ])
      s.engine().onSnapshot(value, 'session-A');
    expect(s.service.history('123').map((event) => event.ruleId)).toEqual([id]);
  });
  it('persists recovery baseline and suppresses repeat alerts across engine restarts', () => {
    const s = setup();
    s.trigger();
    s.setTime(70);
    s.restart();
    s.engine().onSnapshot(snapshot(60, 700), 'session-A');
    s.update(80, 720);
    s.update(90, 740);
    expect(s.db.select().from(alertStates).get()).toMatchObject({
      status: 'WAITING_FOR_RECOVERY',
      baselineVolume: 1000,
    });
    s.update(100, 800); // recovery before cooldown must not rearm
    expect(s.db.select().from(alertStates).get()?.status).toBe(
      'WAITING_FOR_RECOVERY',
    );
    s.update(361, 750); // five percentage points above the trigger boundary, cooldown complete
    expect(s.db.select().from(alertStates).get()?.status).toBe('ARMED');
    s.update(421, 525);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(2);
  });
  it('primes a new session and cannot use history from an earlier session', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, drop);
    s.update(0);
    s.update(50, 1000, 'buy', 'session-B');
    s.update(60, 500, 'buy', 'session-B');
    expect(s.db.select().from(alertEvents).all()).toHaveLength(0);
    const t = setup();
    t.service.create('123', instrument.insCode, gone);
    t.update(0);
    t.update(10, 0, 'none', 'session-B');
    t.update(20, 0, 'none', 'session-B');
    expect(t.db.select().from(alertEvents).all()).toHaveLength(0);
  });
  it('enforces owner authorization for CRUD/history, and resets edited states and outbox', () => {
    const s = setup();
    const id = s.trigger();
    expect(s.service.get('456', id)).toBeNull();
    expect(s.service.setEnabled('456', id, false)).toBe(false);
    expect(s.service.update('456', id, gone)).toBe(false);
    expect(s.service.remove('456', id)).toBe(false);
    expect(s.service.history('456')).toHaveLength(0);
    expect(() => s.service.list('999')).toThrow('Unauthorized');
    expect(() => s.service.history('999')).toThrow('Unauthorized');
    expect(() => s.service.create('999', instrument.insCode, gone)).toThrow();
    expect(s.service.setEnabled('123', id, false)).toBe(true);
    expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
      'CANCELLED',
    );
    expect(s.db.select().from(alertStates).all()).toHaveLength(0);
    s.update(70, 500);
    expect(s.db.select().from(alertStates).all()).toHaveLength(0);
    s.service.setEnabled('123', id, true);
    s.update(80, 500);
    expect(s.db.select().from(alertStates).get()?.status).toBe('ARMED');
    expect(s.service.update('123', id, gone, true, 15)).toBe(true);
    expect(s.service.get('123', id)).toMatchObject({
      definition: gone,
      cooldownSeconds: 15,
    });
    expect(s.service.remove('123', id)).toBe(true);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(0);
    expect(s.db.select().from(notificationOutbox).all()).toHaveLength(0);
  });
  it('preserves independent owners on symbol removal and ignores quarantined/invalid definitions', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, drop);
    const id = s.service.create('456', instrument.insCode, drop);
    s.db
      .insert(alertRules)
      .values({
        id: 'legacy',
        insCode: instrument.insCode,
        type: 'BUY_QUEUE_DISAPPEARED',
        createdAt: start,
      })
      .run();
    s.db
      .insert(alertRules)
      .values({
        id: 'bad',
        ownerId: '456',
        insCode: instrument.insCode,
        type: 'BUY_QUEUE_DISAPPEARED',
        definition: '{',
        createdAt: start,
      })
      .run();
    s.users.removeSymbol('123', instrument.insCode);
    s.update(0);
    s.update(60, 700);
    expect(s.service.history('123')).toHaveLength(0);
    expect(s.service.history('456').map((e) => e.ruleId)).toEqual([id]);
    expect(s.db.select().from(alertStates).all()).toHaveLength(1);
  });
  it('rolls back event, outbox and state together on a persistence failure', () => {
    const s = setup();
    s.service.create('123', instrument.insCode, drop);
    s.update(0);
    s.sqlite.exec(
      "CREATE TRIGGER reject_outbox BEFORE INSERT ON notification_outbox BEGIN SELECT RAISE(ABORT, 'test failure'); END",
    );
    expect(() => s.update(60, 700)).not.toThrow();
    expect(s.db.select().from(alertEvents).all()).toHaveLength(0);
    expect(s.db.select().from(alertStates).get()?.lastEvaluatedAt).toBe(start);
    s.sqlite.exec('DROP TRIGGER reject_outbox');
    s.engine().onSnapshot(snapshot(60, 700), 'session-A');
    expect(s.db.select().from(alertEvents).all()).toHaveLength(1);
  });
});

describe('notification outbox', () => {
  it('measures retry_after from the failure response and cancels in-flight sends on stop', async () => {
    const s = setup();
    s.trigger();
    const slowFailure = vi.fn<SendNotification>().mockImplementation(() => {
      s.setTime(70);
      return Promise.reject(
        Object.assign(new Error('Rate limited'), {
          error_code: 429,
          parameters: { retry_after: 10 },
        }),
      );
    });
    await s.dispatcher(slowFailure).dispatchOnce();
    expect(s.db.select().from(notificationOutbox).get()?.nextAttemptAt).toBe(
      start + 80_000,
    );
    const t = setup();
    t.trigger();
    const send = vi
      .fn<SendNotification>()
      .mockImplementation(() => new Promise(() => {}));
    const worker = t.dispatcher(send);
    const active = worker.dispatchOnce();
    await worker.stop();
    await active;
    expect(send.mock.calls[0]?.[2].aborted).toBe(true);
    expect(t.db.select().from(notificationOutbox).get()?.status).toBe(
      'PENDING',
    );
  });
  it('delivers plain Persian messages to the observed owner chat once', async () => {
    const s = setup();
    s.trigger();
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    const worker = s.dispatcher(send);
    await worker.dispatchOnce();
    await worker.dispatchOnce();
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0]?.[0]).toBe('123');
    expect(send.mock.calls[0]?.[1]).toContain('خودرو');
    expect(send.mock.calls[0]?.[1]).toContain('۳۰٪');
    expect(send.mock.calls[0]?.[1]).toContain('تهران');
    expect(s.db.select().from(notificationOutbox).get()).toMatchObject({
      status: 'DELIVERED',
      attempts: 1,
    });
    expect(s.db.select().from(alertEvents).get()?.sentAt).toBe(s.clock());
  });
  it('honors retry_after across worker restart, then delivers', async () => {
    const s = setup();
    s.trigger();
    const send = vi
      .fn<SendNotification>()
      .mockRejectedValueOnce({
        error_code: 429,
        parameters: { retry_after: 10 },
      })
      .mockResolvedValue({});
    await s.dispatcher(send).dispatchOnce();
    s.setTime(69);
    await s.dispatcher(send).dispatchOnce();
    expect(send).toHaveBeenCalledTimes(1);
    s.setTime(70);
    await s.dispatcher(send).dispatchOnce();
    expect(send).toHaveBeenCalledTimes(2);
    expect(s.db.select().from(notificationOutbox).get()).toMatchObject({
      status: 'DELIVERED',
      attempts: 2,
    });
  });
  it('uses exponential transport/server retries and stops at the configured limit', async () => {
    const s = setup();
    s.trigger();
    const send = vi
      .fn<SendNotification>()
      .mockRejectedValue({ error_code: 503 });
    const worker = s.dispatcher(send, 2);
    await worker.dispatchOnce();
    expect(s.db.select().from(notificationOutbox).get()?.nextAttemptAt).toBe(
      start + 62_000,
    );
    s.setTime(62);
    await worker.dispatchOnce();
    expect(s.db.select().from(notificationOutbox).get()?.nextAttemptAt).toBe(
      start + 66_000,
    );
    s.setTime(66);
    await worker.dispatchOnce();
    expect(s.db.select().from(notificationOutbox).get()).toMatchObject({
      status: 'FAILED',
      attempts: 3,
      errorCode: 'BALE_503',
    });
    s.setTime(100);
    await worker.dispatchOnce();
    expect(send).toHaveBeenCalledTimes(3);
  });
  it.each([400, 401, 403])(
    'does not retry permanent Bale %s errors',
    async (code) => {
      const s = setup();
      s.trigger();
      const send = vi
        .fn<SendNotification>()
        .mockRejectedValue({ error_code: code });
      const worker = s.dispatcher(send);
      await worker.dispatchOnce();
      s.setTime(100);
      await worker.dispatchOnce();
      expect(send).toHaveBeenCalledTimes(1);
      expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
        'FAILED',
      );
    },
  );
  it('rechecks notification preferences, active ownership, watchlist and rule revision', async () => {
    for (const action of [
      'preferences',
      'disabled',
      'revision',
      'allowlist',
    ] as const) {
      const s = setup();
      const id = s.trigger();
      const send = vi.fn<SendNotification>().mockResolvedValue({});
      if (action === 'preferences') s.users.setNotifications('123', false);
      if (action === 'disabled')
        s.db
          .update(alertRules)
          .set({ enabled: false })
          .where(eq(alertRules.id, id))
          .run();
      if (action === 'revision')
        s.db
          .update(alertRules)
          .set({ revision: 99 })
          .where(eq(alertRules.id, id))
          .run();
      const worker =
        action === 'allowlist'
          ? new NotificationDispatcher(
              s.db,
              new UserService(s.db, ['456']),
              send,
              {
                maxRetries: 5,
                retryBaseMs: 2000,
                maxAgeMs: 900_000,
                marketMaxAgeMs: 30_000,
              },
              createLogger('silent'),
              s.clock,
            )
          : s.dispatcher(send);
      await worker.dispatchOnce();
      expect(send).not.toHaveBeenCalled();
      expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
        'CANCELLED',
      );
    }
  });
  it('expires obsolete notifications and marks delayed delivery', async () => {
    const s = setup();
    s.trigger();
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    s.setTime(961);
    await s.dispatcher(send).dispatchOnce();
    expect(send).not.toHaveBeenCalled();
    expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
      'EXPIRED',
    );
    const t = setup();
    t.trigger();
    t.setTime(91);
    await t.dispatcher(send).dispatchOnce();
    expect(send.mock.calls[0]?.[1]).toContain('با تأخیر');
  });
  it('claims atomically across concurrent workers and recovers an abandoned lease', async () => {
    const s = setup();
    s.trigger();
    let resolve: (() => void) | undefined;
    const send = vi.fn<SendNotification>().mockImplementation(
      () =>
        new Promise<void>((done) => {
          resolve = done;
        }),
    );
    const first = s.dispatcher(send).dispatchOnce();
    await s.dispatcher(send).dispatchOnce();
    expect(send).toHaveBeenCalledTimes(1);
    resolve?.();
    await first;
    const t = setup();
    t.trigger();
    t.db
      .update(notificationOutbox)
      .set({
        status: 'SENDING',
        leaseUntil: start + 120_000,
        leaseToken: 'dead-worker',
        attempts: 1,
      })
      .run();
    const recovered = vi.fn<SendNotification>().mockResolvedValue({});
    await t.dispatcher(recovered).dispatchOnce();
    expect(recovered).not.toHaveBeenCalled();
    t.setTime(120);
    await t.dispatcher(recovered).dispatchOnce();
    expect(recovered).toHaveBeenCalledTimes(1);
    expect(t.db.select().from(notificationOutbox).get()?.status).toBe(
      'DELIVERED',
    );
  });
  it('bounds a hung send and joins shutdown without losing pending work', async () => {
    vi.useFakeTimers();
    const s = setup();
    s.trigger();
    const send = vi
      .fn<SendNotification>()
      .mockImplementation(() => new Promise(() => {}));
    const worker = s.dispatcher(send);
    const pending = worker.dispatchOnce();
    await vi.advanceTimersByTimeAsync(10_000);
    await pending;
    expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
      'PENDING',
    );
    const next = worker.dispatchOnce();
    await worker.stop();
    await next;
  });
  it('formats arbitrary symbols as plain text and includes source time and actual values', () => {
    const text = formatNotification(
      {
        symbol: '<x>_*',
        type: 'SELL_QUEUE_DISAPPEARED',
        timestamp: start,
        previousVolume: 100,
        currentVolume: 0,
        actualPercent: 100,
        windowMinutes: null,
      },
      start,
      30_000,
    );
    expect(text).toContain('<x>_*');
    expect(text).toContain('ناپدید شدن صف فروش');
    expect(text).toContain('۱۰۰٪');
    expect(text).not.toContain('با تأخیر');
  });
});
