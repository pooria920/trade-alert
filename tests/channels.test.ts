import { afterEach, describe, expect, it, vi } from 'vitest';
import { eq } from 'drizzle-orm';
import { openDatabase } from '../src/database/client.js';
import {
  alertEvents,
  alertRules,
  alertStates,
  notificationOutbox,
  telegramChannels,
} from '../src/database/schema.js';
import {
  ChannelService,
  type ChannelVerification,
} from '../src/alerts/channel-service.js';
import { AlertService } from '../src/alerts/alert-service.js';
import { AlertEngine } from '../src/alerts/alert-engine.js';
import {
  channelDefinitionSchema,
  combine,
  evaluateTree,
  type RuleTree,
  type RuleCondition,
} from '../src/alerts/composite.js';
import { SnapshotRepository } from '../src/database/repositories/snapshot.repository.js';
import { UserService } from '../src/users/user-service.js';
import type { QueueSnapshot } from '../src/market/market.types.js';
import {
  NotificationDispatcher,
  type SendNotification,
} from '../src/notifications/notification-dispatcher.js';
import { formatNotification } from '../src/notifications/notifier.js';
import { createLogger } from '../src/shared/logger.js';
import { instrument } from './fixtures/normalized.js';
import { parseEnv } from '../src/config/env.js';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Bot } from 'grammy';
import { channelVerifier } from '../src/notifications/channel-permissions.js';

const channelId = '-1001234567890';
const start = Date.parse('2026-10-21T06:30:00Z');
const drop: RuleCondition = {
  type: 'BUY_QUEUE_DROP_PERCENT',
  config: { thresholdPercent: 30, windowMinutes: 1, minimumBaselineVolume: 0 },
};
const volume: RuleCondition = {
  type: 'BUY_QUEUE_VOLUME_ABOVE',
  config: { minimumVolume: 500 },
};
const unknown: RuleCondition = {
  type: 'SELL_QUEUE_DROP_PERCENT',
  config: { thresholdPercent: 30, windowMinutes: 5, minimumBaselineVolume: 0 },
};
const group = (
  operator: 'AND' | 'OR',
  conditions: RuleTree[] = [drop, volume],
): RuleTree => ({ type: 'GROUP', operator, conditions });
const snapshot = (
  seconds: number,
  quantity = 1000,
  type: QueueSnapshot['queueType'] = 'buy',
): QueueSnapshot => ({
  insCode: instrument.insCode,
  symbol: instrument.symbol,
  timestamp: start + seconds * 1000,
  queueType: type,
  volume: type === 'none' ? 0 : quantity,
  orderCount: type === 'none' ? 0 : 1,
  price: type === 'none' ? null : 110,
});
const connections: ReturnType<typeof openDatabase>[] = [];
afterEach(() => {
  for (const connection of connections.splice(0))
    if (connection.sqlite.open) connection.sqlite.close();
  vi.useRealTimers();
});
function setup(path = ':memory:') {
  const connection = openDatabase(path);
  connections.push(connection);
  const { db } = connection;
  const users = new UserService(db, ['123', '456', '789']);
  for (const id of ['123', '456', '789']) users.register(id, id);
  let now = start;
  const clock = () => now;
  const verify = vi
    .fn<ChannelVerification>()
    .mockResolvedValue({ channelId, title: 'کانال آزمایشی', canPost: true });
  const channels = new ChannelService(
    db,
    users,
    ['123', '456'],
    channelId,
    verify,
    0,
    clock,
  );
  const privateAlerts = new AlertService(db, users, 0, 10, clock);
  const repo = new SnapshotRepository(db);
  const logger = createLogger('silent');
  let engine = new AlertEngine(
    db,
    repo,
    users,
    ['123', '456', '789'],
    30_000,
    logger,
    clock,
    channels,
  );
  const update = (
    seconds: number,
    quantity = 1000,
    type: QueueSnapshot['queueType'] = 'buy',
  ) => {
    now = start + seconds * 1000;
    const value = snapshot(seconds, quantity, type);
    repo.save(value, now, 'session-A');
    engine.onSnapshot(value, 'session-A');
  };
  const dispatcher = (send: SendNotification) =>
    new NotificationDispatcher(
      db,
      users,
      send,
      {
        maxRetries: 5,
        retryBaseMs: 2000,
        maxAgeMs: 900_000,
        marketMaxAgeMs: 30_000,
      },
      logger,
      clock,
      channels,
    );
  return {
    ...connection,
    users,
    channels,
    verify,
    privateAlerts,
    repo,
    update,
    dispatcher,
    clock,
    setTime: (seconds: number) => {
      now = start + seconds * 1000;
    },
    restart: () => {
      engine = new AlertEngine(
        db,
        repo,
        users,
        ['123', '456', '789'],
        30_000,
        logger,
        clock,
        channels,
      );
    },
  };
}

describe('composite three-valued evaluation', () => {
  it('reuses drop arithmetic and combines confirmed queue-volume conditions', () => {
    const history = () => snapshot(0);
    const current = snapshot(60, 700);
    expect(evaluateTree(group('AND'), current, history)).toMatchObject({
      truth: 'TRUE',
      matches: [{ actualPercent: 30 }, { currentVolume: 700 }],
    });
    expect(evaluateTree(group('AND'), snapshot(60, 400), history).truth).toBe(
      'FALSE',
    );
    expect(evaluateTree(group('OR'), snapshot(60, 400), history).truth).toBe(
      'TRUE',
    );
    expect(evaluateTree(group('AND'), current, () => null).truth).toBe(
      'UNKNOWN',
    );
  });
  it('uses explicit Kleene logic; never promotes overall UNKNOWN to a trigger', () => {
    expect(combine('AND', ['TRUE', 'UNKNOWN'])).toBe('UNKNOWN');
    expect(combine('AND', ['FALSE', 'UNKNOWN'])).toBe('FALSE');
    expect(combine('OR', ['TRUE', 'UNKNOWN'])).toBe('TRUE');
    expect(combine('OR', ['FALSE', 'UNKNOWN'])).toBe('UNKNOWN');
    expect(
      evaluateTree(group('AND', [drop, unknown]), snapshot(60, 700), () =>
        snapshot(0),
      ).truth,
    ).toBe('UNKNOWN');
    expect(
      evaluateTree(group('OR', [drop, unknown]), snapshot(60, 700), () =>
        snapshot(0),
      ).truth,
    ).toBe('TRUE');
    expect(
      evaluateTree(volume, snapshot(60, 0, 'none'), () => null).truth,
    ).toBe('FALSE');
    expect(
      evaluateTree(volume, snapshot(60, 700, 'sell'), () => null).truth,
    ).toBe('UNKNOWN');
  });
  it('supports nested groups against the same snapshot', () => {
    const tree = group('AND', [volume, group('OR', [unknown, drop])]);
    expect(evaluateTree(tree, snapshot(60, 700), () => snapshot(0)).truth).toBe(
      'TRUE',
    );
  });
  it('rejects empty/oversized/deep/unsupported/invalid condition trees', () => {
    let deep: RuleTree = drop;
    for (let index = 0; index < 5; index++) deep = group('AND', [deep]);
    for (const value of [
      group('AND', []),
      group(
        'OR',
        Array.from({ length: 9 }, () => drop),
      ),
      deep,
      { type: 'GROUP', operator: 'NOT', conditions: [drop] },
      { type: 'PRICE_CHANGE', config: {} },
      { type: 'BUY_QUEUE_VOLUME_ABOVE', config: { minimumVolume: -1 } },
    ])
      expect(channelDefinitionSchema.safeParse(value).success).toBe(false);
  });
});

describe('channel rules and isolation', () => {
  it('recovers a persisted channel event and composite state after closing and reopening SQLite', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'tse-channel-restart-'));
    const path = join(directory, 'channel.db');
    try {
      const first = setup(path);
      await first.channels.refresh();
      const id = first.channels.create('123', instrument, group('AND'));
      first.update(0);
      first.update(60, 700);
      first.sqlite.close();
      const second = setup(path);
      second.setTime(70);
      second.restart();
      expect(second.channels.get('456', id)).not.toBeNull();
      const send = vi.fn<SendNotification>().mockResolvedValue({});
      await second.dispatcher(send).dispatchOnce();
      await second.dispatcher(send).dispatchOnce();
      expect(send.mock.calls.map((call) => call[0])).toEqual([channelId]);
      second.update(80, 800);
      expect(second.db.select().from(alertEvents).all()).toHaveLength(1);
      expect(second.db.select().from(alertStates).get()?.status).toBe('ARMED');
      const lastSuccess = second.channels.status('456').lastSuccess;
      second.channels.remove('456', id);
      expect(second.channels.status('123').lastSuccess).toBe(lastSuccess);
      expect(second.sqlite.pragma('foreign_key_check')).toEqual([]);
      second.sqlite.close();
    } finally {
      for (const connection of connections)
        if (connection.sqlite.open) connection.sqlite.close();
      rmSync(directory, { recursive: true, force: true });
    }
  });
  it('requires registered configured admins and recent verified posting permissions', async () => {
    const s = setup();
    expect(() => s.channels.create('123', instrument, group('AND'))).toThrow(
      'permission',
    );
    await s.channels.refresh();
    for (const actor of ['789', '999']) {
      expect(() => s.channels.list(actor)).toThrow('not authorized');
      expect(() => s.channels.create(actor, instrument, group('AND'))).toThrow(
        'not authorized',
      );
      expect(() => s.channels.remove(actor, 'anything')).toThrow(
        'not authorized',
      );
      expect(() => s.channels.status(actor)).toThrow('not authorized');
    }
    s.setTime(61);
    expect(() => s.channels.create('123', instrument, drop)).toThrow(
      'permission',
    );
    s.verify.mockResolvedValue({
      channelId,
      title: 'بدون اجازه',
      canPost: false,
    });
    expect(await s.channels.refresh()).toBe(false);
    expect(() => s.channels.create('123', instrument, drop)).toThrow(
      'permission',
    );
    expect(s.channels.status('123')).toMatchObject({
      canPost: false,
      total: 0,
    });
  });
  it('manages multiple independent same-symbol rules and preserves private rules and memberships', async () => {
    const s = setup();
    await s.channels.refresh();
    s.users.addSymbol('123', instrument);
    const privateId = s.privateAlerts.create('123', instrument.insCode, {
      type: 'BUY_QUEUE_DISAPPEARED',
      config: { minimumPreviousVolume: 0 },
    });
    const first = s.channels.create('123', instrument, group('AND'));
    const second = s.channels.create('456', instrument, group('OR'));
    expect(s.channels.monitoredSymbols()).toHaveLength(1);
    expect(s.privateAlerts.list('123').map((r) => r.id)).toEqual([privateId]);
    expect(s.privateAlerts.remove('123', first)).toBe(false);
    expect(s.privateAlerts.get('123', first)).toBeNull();
    expect(s.users.notificationTarget('123', first)).toBeNull();
    expect(s.channels.get('123', privateId)).toBeNull();
    expect(s.channels.remove('123', privateId)).toBe(false);
    expect(s.channels.setEnabled('456', first, false)).toBe(true);
    expect(s.channels.get('123', first)).toMatchObject({
      enabled: false,
      ownerId: '456',
    });
    expect(s.channels.update('123', first, group('OR'), true, 1)).toBe(false);
    expect(s.channels.update('123', first, group('OR'), true, 2)).toBe(true);
    s.users.removeSymbol('123', instrument.insCode);
    expect(s.channels.list('123').map((r) => r.id)).toEqual(
      expect.arrayContaining([first, second]),
    );
    expect(s.channels.remove('456', first)).toBe(true);
    expect(s.channels.list('123').map((r) => r.id)).toEqual([second]);
    expect(s.sqlite.pragma('foreign_key_check')).toEqual([]);
  });
  it('routes one event per rule to the channel, independent of private preferences/subscribers', async () => {
    const s = setup();
    await s.channels.refresh();
    s.users.setNotifications('123', false);
    s.channels.create('123', instrument, group('AND'));
    s.channels.create('456', instrument, group('OR'));
    s.users.addSymbol('789', instrument);
    s.privateAlerts.create('789', instrument.insCode, {
      type: 'BUY_QUEUE_DROP_PERCENT',
      config: {
        thresholdPercent: 30,
        windowMinutes: 1,
        minimumBaselineVolume: 0,
      },
    });
    s.update(0);
    s.update(60, 700);
    s.update(60, 700);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(3);
    expect(s.privateAlerts.history('123')).toHaveLength(0);
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    const worker = s.dispatcher(send);
    for (let i = 0; i < 4; i++) await worker.dispatchOnce();
    expect(send.mock.calls.map((call) => call[0]).sort()).toEqual(
      [channelId, channelId, '789'].sort(),
    );
    expect(
      s.db
        .select()
        .from(notificationOutbox)
        .all()
        .every((row) => row.status === 'DELIVERED'),
    ).toBe(true);
    expect(s.verify).toHaveBeenCalledTimes(3); // startup check plus two channel sends
    const channelText =
      send.mock.calls.find((call) => call[0] === channelId)?.[1] ?? '';
    expect(channelText).toContain('خودرو');
    expect(channelText).toContain('۳۰٪');
    expect(channelText).toContain('تهران');
    expect(channelText).not.toContain('rule');
    expect(channelText).not.toContain('123456');
    expect(s.channels.status('123')).toMatchObject({
      total: 2,
      active: 2,
      lastSuccess: s.clock(),
    });
  });
  it('retains composite baselines across restart, requires whole-rule recovery and suppresses duplicates', async () => {
    const s = setup();
    await s.channels.refresh();
    s.channels.create('123', instrument, group('OR'));
    s.update(0);
    s.update(60, 700);
    s.setTime(70);
    s.restart();
    s.update(80, 760); // drop recovered, but OR volume condition is still true
    expect(s.db.select().from(alertStates).get()?.status).toBe(
      'WAITING_FOR_RECOVERY',
    );
    s.update(90, 450); // volume recovered, but drop remains active
    expect(s.db.select().from(alertStates).get()?.status).toBe(
      'WAITING_FOR_RECOVERY',
    );
    expect(s.db.select().from(alertEvents).all()).toHaveLength(1);
    // An AND group can recover when its volume condition is conclusively false.
    const t = setup();
    await t.channels.refresh();
    t.channels.create('123', instrument, group('AND'));
    t.update(0);
    t.update(60, 700);
    t.update(70, 450);
    expect(t.db.select().from(alertStates).get()?.status).toBe('ARMED');
    t.update(80, 700); // same rolling baseline still present within tolerance, new confirmed occurrence
    expect(t.db.select().from(alertEvents).all()).toHaveLength(2);
  });
  it('does not publish overall UNKNOWN or compare across sessions', async () => {
    const s = setup();
    await s.channels.refresh();
    s.channels.create('123', instrument, group('AND', [drop, unknown]));
    s.update(0);
    s.update(60, 700);
    expect(s.db.select().from(alertEvents).all()).toHaveLength(0);
  });
  it('fails channel permission errors safely, preserves private delivery, and records admin-only status', async () => {
    const s = setup();
    await s.channels.refresh();
    s.channels.create('123', instrument, group('AND'));
    s.update(0);
    s.update(60, 700);
    s.verify.mockResolvedValue({
      channelId,
      title: 'کانال آزمایشی',
      canPost: false,
    });
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    await s.dispatcher(send).dispatchOnce();
    expect(send).not.toHaveBeenCalled();
    expect(s.db.select().from(notificationOutbox).get()).toMatchObject({
      status: 'FAILED',
      errorCode: 'BALE_403',
    });
    expect(s.channels.status('123')).toMatchObject({
      canPost: false,
      lastError: { code: 'BALE_403' },
    });
    expect(() => s.channels.status('789')).toThrow();
  });
  it('retries temporary verification failures and resumes a pending channel outbox after worker restart', async () => {
    const s = setup();
    await s.channels.refresh();
    s.channels.create('123', instrument, group('AND'));
    s.update(0);
    s.update(60, 700);
    s.verify.mockRejectedValueOnce(new Error('Mock network failure'));
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    await s.dispatcher(send).dispatchOnce();
    expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
      'PENDING',
    );
    s.setTime(62);
    await s.dispatcher(send).dispatchOnce();
    expect(send).toHaveBeenCalledTimes(1);
    expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
      'DELIVERED',
    );
  });
  it.each(['creator', 'disabled', 'revision', 'channel'])(
    'cancels ineligible channel delivery: %s',
    async (reason) => {
      const s = setup();
      await s.channels.refresh();
      const id = s.channels.create('123', instrument, group('AND'));
      s.update(0);
      s.update(60, 700);
      const send = vi.fn<SendNotification>().mockResolvedValue({});
      if (reason === 'creator')
        s.db
          .update(alertRules)
          .set({ ownerId: '789' })
          .where(eq(alertRules.id, id))
          .run();
      if (reason === 'disabled')
        s.db
          .update(alertRules)
          .set({ enabled: false })
          .where(eq(alertRules.id, id))
          .run();
      if (reason === 'revision')
        s.db
          .update(alertRules)
          .set({ revision: 99 })
          .where(eq(alertRules.id, id))
          .run();
      if (reason === 'channel') {
        const other = '-1009876543210';
        s.db.insert(telegramChannels).values({ channelId: other }).run();
        s.db
          .update(alertRules)
          .set({ channelId: other })
          .where(eq(alertRules.id, id))
          .run();
      }
      await s.dispatcher(send).dispatchOnce();
      expect(send).not.toHaveBeenCalled();
      expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
        'CANCELLED',
      );
      if (reason !== 'revision')
        expect(s.channels.monitoredSymbols()).toEqual([]);
    },
  );
  it('rechecks source age after permission verification and applies cooldown to the whole group', async () => {
    const s = setup();
    await s.channels.refresh();
    s.channels.create('123', instrument, group('AND'));
    s.update(0);
    s.update(60, 700);
    s.verify.mockImplementationOnce(() => {
      s.setTime(961);
      return Promise.resolve({
        channelId,
        title: 'کانال آزمایشی',
        canPost: true,
      });
    });
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    await s.dispatcher(send).dispatchOnce();
    expect(send).not.toHaveBeenCalled();
    expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
      'EXPIRED',
    );
    const t = setup();
    await t.channels.refresh();
    const id = t.channels.create('123', instrument, group('AND'));
    t.db
      .update(alertRules)
      .set({ cooldownSeconds: 300 })
      .where(eq(alertRules.id, id))
      .run();
    t.update(0);
    t.update(60, 700);
    t.update(70, 450);
    t.update(300, 1000);
    expect(t.db.select().from(alertStates).get()?.status).toBe(
      'WAITING_FOR_RECOVERY',
    );
    t.update(361, 450);
    expect(t.db.select().from(alertStates).get()?.status).toBe('ARMED');
    t.update(362, 1000);
    t.update(422, 700);
    expect(t.db.select().from(alertEvents).all()).toHaveLength(2);
  });
  it('rechecks revision after asynchronous permission checks', async () => {
    const s = setup();
    await s.channels.refresh();
    const id = s.channels.create('123', instrument, group('AND'));
    s.update(0);
    s.update(60, 700);
    s.verify.mockImplementationOnce(() => {
      s.channels.remove('456', id);
      return Promise.resolve({
        channelId,
        title: 'کانال آزمایشی',
        canPost: true,
      });
    });
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    await s.dispatcher(send).dispatchOnce();
    expect(send).not.toHaveBeenCalled();
    expect(s.db.select().from(notificationOutbox).all()).toHaveLength(0);
  });
  it('bounds permission checks as part of the leased send deadline', async () => {
    vi.useFakeTimers();
    const s = setup();
    await s.channels.refresh();
    s.channels.create('123', instrument, group('AND'));
    s.update(0);
    s.update(60, 700);
    s.verify.mockImplementationOnce(() => new Promise(() => {}));
    const send = vi.fn<SendNotification>();
    const worker = s.dispatcher(send);
    const pending = worker.dispatchOnce();
    await vi.advanceTimersByTimeAsync(10_000);
    await pending;
    await worker.stop();
    expect(send).not.toHaveBeenCalled();
    expect(s.db.select().from(notificationOutbox).get()?.status).toBe(
      'PENDING',
    );
  });
});

describe('channel configuration and plain messages', () => {
  it('delivers to a configured group and rejects delivery after bot administrator rights are revoked', async () => {
    const s = setup();
    const bot = new Bot(`123456:${'x'.repeat(35)}`);
    let administrator = true;
    bot.api.config.use((previous, method) =>
      Promise.resolve({
        ok: true,
        result:
          method === 'getChat'
            ? { id: Number(channelId), type: 'group', title: 'گروه آزمایشی' }
            : { status: administrator ? 'administrator' : 'member' },
      } as Awaited<ReturnType<typeof previous>>),
    );
    s.verify.mockImplementation(channelVerifier(bot.api, () => 123456));
    expect(await s.channels.refresh()).toBe(true);
    s.channels.create('123', instrument, group('AND'));
    s.channels.create('123', instrument, group('AND'));
    s.update(0);
    s.update(60, 700);
    const send = vi.fn<SendNotification>().mockResolvedValue({});
    const worker = s.dispatcher(send);
    await worker.dispatchOnce();
    expect(send).toHaveBeenCalledOnce();
    expect(send.mock.calls[0]?.[0]).toBe(channelId);
    administrator = false;
    await worker.dispatchOnce();
    expect(send).toHaveBeenCalledOnce();
    expect(
      s.db
        .select()
        .from(notificationOutbox)
        .all()
        .map((row) => row.status)
        .sort(),
    ).toEqual(['DELIVERED', 'FAILED']);
  });

  it.each([
    {
      chatType: 'group',
      member: { status: 'administrator' },
      expected: true,
    },
    {
      chatType: 'group',
      member: { status: 'member' },
      expected: false,
    },
    {
      chatType: 'group',
      member: { status: 'restricted', can_send_messages: true },
      expected: false,
    },
    {
      chatType: 'private',
      member: { status: 'administrator' },
      expected: false,
    },
    {
      chatType: 'channel',
      member: { status: 'administrator', can_post_messages: true },
      expected: true,
    },
    {
      chatType: 'channel',
      member: { status: 'administrator', can_post_messages: false },
      expected: false,
    },
    {
      chatType: 'channel',
      member: { status: 'member', can_post_messages: true },
      expected: false,
    },
    {
      chatType: 'supergroup',
      member: { status: 'administrator', can_post_messages: true },
      expected: false,
    },
  ])(
    'checks actual Bale chat kind, administrator status and posting right: %j',
    async ({ chatType, member, expected }) => {
      const bot = new Bot(`123456:${'x'.repeat(35)}`);
      const methods: string[] = [];
      bot.api.config.use((previous, method) => {
        methods.push(method);
        return Promise.resolve({
          ok: true,
          result:
            method === 'getChat'
              ? {
                  id: Number(channelId),
                  type: chatType,
                  title: 'کانال آزمایشی',
                }
              : member,
        } as Awaited<ReturnType<typeof previous>>);
      });
      expect(
        await channelVerifier(bot.api, () => 123456)(channelId),
      ).toMatchObject({ canPost: expected });
      expect(methods).toEqual(
        chatType === 'channel' || chatType === 'group'
          ? ['getChat', 'getChatMember']
          : ['getChat'],
      );
    },
  );
  const required = {
    BALE_BOT_TOKEN: `123456:${'x'.repeat(35)}`,
    BALE_ALLOWED_USER_IDS: '123,456,789',
  };
  it('keeps private-only configuration working and validates separate admins/channel together', () => {
    expect(parseEnv(required)).toMatchObject({
      BALE_ADMIN_USER_IDS: [],
      BALE_CHANNEL_ID: '',
    });
    expect(
      parseEnv({
        ...required,
        BALE_ADMIN_USER_IDS: '123,456,123',
        BALE_CHANNEL_ID: channelId,
      }).BALE_ADMIN_USER_IDS,
    ).toEqual(['123', '456']);
    for (const invalid of [
      { BALE_ADMIN_USER_IDS: '123' },
      { BALE_CHANNEL_ID: channelId },
      { BALE_ADMIN_USER_IDS: '123', BALE_CHANNEL_ID: '@arbitrary' },
      { BALE_ADMIN_USER_IDS: '123,-1', BALE_CHANNEL_ID: channelId },
    ])
      expect(() => parseEnv({ ...required, ...invalid })).toThrow(
        'Invalid environment',
      );
  });
  it('formats satisfied composite evidence without internal identifiers or markup parsing', () => {
    const result = evaluateTree(group('AND'), snapshot(60, 700), () =>
      snapshot(0),
    );
    const text = formatNotification(
      {
        type: 'COMPOSITE',
        operator: 'AND',
        symbol: '<نماد>_*',
        timestamp: start + 60_000,
        matches: result.matches,
      },
      start + 100_000,
      30_000,
    );
    expect(text).toContain('<نماد>_*');
    expect(text).toContain('همه شرط‌ها');
    expect(text).toContain('۳۰٪');
    expect(text).toContain('با تأخیر');
  });
});
