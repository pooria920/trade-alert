import type { Update, UserFromGetMe } from 'grammy/types';
import { createRequire } from 'node:module';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createBot } from '../src/bot/bot.js';
import { parseEnv } from '../src/config/env.js';
import { createLogger } from '../src/shared/logger.js';
import { openDatabase } from '../src/database/client.js';
import { UserService } from '../src/users/user-service.js';
import { MarketService } from '../src/market/market-service.js';
import { makeBook, instrument } from './fixtures/normalized.js';
import { makeStatus } from './fixtures/monitoring.js';
import { AlertService } from '../src/alerts/alert-service.js';
import { alertEvents } from '../src/database/schema.js';
import { ChannelService } from '../src/alerts/channel-service.js';
import { channelVerifier } from '../src/notifications/channel-permissions.js';

const allowedUserId = 123456789;
const botInfo: UserFromGetMe = {
  id: 123456,
  is_bot: true,
  first_name: 'Test',
  username: 'test_bot',
  can_join_groups: false,
  can_read_all_group_messages: false,
  supports_inline_queries: false,
  can_connect_to_business: false,
  has_main_web_app: false,
  has_topics_enabled: false,
  allows_users_to_create_topics: false,
  can_manage_bots: false,
  supports_join_request_queries: false,
};

const connections: ReturnType<typeof openDatabase>[] = [];
afterEach(() => {
  vi.restoreAllMocks();
  for (const connection of connections.splice(0)) connection.sqlite.close();
});

function setup(
  channelMode = false,
  interceptApi = true,
  chatType: 'channel' | 'group' = 'channel',
) {
  const connection = openDatabase(':memory:');
  connections.push(connection);
  const users = new UserService(connection.db, [
    String(allowedUserId),
    '987654321',
    '456789123',
  ]);
  const provider = {
    search: vi.fn().mockResolvedValue([instrument]),
    snapshot: vi.fn().mockImplementation(() => Promise.resolve(makeBook())),
  };
  const alerts = new AlertService(connection.db, users);
  const channelId = '-1001234567890';
  const channels = new ChannelService(
    connection.db,
    users,
    channelMode ? [String(allowedUserId), '987654321'] : [],
    channelMode ? channelId : '',
    (id, signal) => channelVerifier(bot.api, () => bot.botInfo.id)(id, signal),
  );
  const bot = createBot(
    parseEnv({
      BALE_BOT_TOKEN: `123456:${'x'.repeat(35)}`,
      BALE_ALLOWED_USER_IDS: `${allowedUserId},987654321,456789123`,
      BALE_ADMIN_USER_IDS: channelMode ? `${allowedUserId},987654321` : '',
      BALE_CHANNEL_ID: channelMode ? channelId : '',
    }),
    createLogger('silent'),
    {
      users,
      alerts,
      market: new MarketService(provider),
      monitor: { getStatus: () => makeStatus() },
      ...(channelMode ? { channels } : {}),
    },
  );
  bot.botInfo = botInfo;
  const calls: { method: string; payload: unknown }[] = [];
  let canPost = true;
  // Intercept grammY at its API boundary: no network calls are made.
  if (interceptApi)
    bot.api.config.use((previous, method, payload) => {
      calls.push({ method, payload });
      const result =
        method === 'getChat'
          ? { id: Number(channelId), type: chatType, title: 'مقصد آزمایشی' }
          : method === 'getChatMember'
            ? {
                status:
                  chatType === 'group' && !canPost ? 'member' : 'administrator',
                ...(chatType === 'channel'
                  ? { can_post_messages: canPost }
                  : {}),
              }
            : true;
      return Promise.resolve({ ok: true, result } as Awaited<
        ReturnType<typeof previous>
      >);
    });
  return {
    bot,
    calls,
    users,
    provider,
    alerts,
    db: connection.db,
    channels,
    setCanPost: (value: boolean) => {
      canPost = value;
    },
  };
}

function command(userId: number, text = '/start', group = false): Update {
  return {
    update_id: 1,
    message: {
      message_id: 1,
      date: 1_700_000_000,
      from: { id: userId, is_bot: false, first_name: 'User' },
      chat: group
        ? { id: -1001, type: 'group', title: 'Test' }
        : { id: userId, type: 'private', first_name: 'User' },
      text,
      entities: [
        {
          type: 'bot_command',
          offset: 0,
          length: text.split(' ')[0]?.length ?? text.length,
        },
      ],
    },
  };
}

function callback(userId: number, data = 'help', chatId = userId): Update {
  return {
    update_id: 2,
    callback_query: {
      id: 'callback-1',
      from: { id: userId, is_bot: false, first_name: 'User' },
      chat_instance: 'test',
      data,
      message: {
        message_id: 1,
        date: 1,
        chat: { id: chatId, type: 'private', first_name: 'Owner' },
      },
    },
  };
}

describe('Bale authorization and commands', () => {
  it('uses Bale HTTPS and strips Telegram-only polling parameters at the HTTP boundary', async () => {
    const require = createRequire(import.meta.url);
    const grammyRequire = createRequire(require.resolve('grammy'));
    const transport = grammyRequire('node-fetch') as { default: typeof fetch };
    const requests: { url: string; payload: unknown }[] = [];
    vi.spyOn(transport, 'default').mockImplementation((input, options) => {
      const url =
        input instanceof URL
          ? input.href
          : typeof input === 'string'
            ? input
            : input.url;
      const method = url.split('/').at(-1);
      if (typeof options?.body !== 'string')
        throw new Error('Expected JSON body');
      requests.push({
        url,
        payload: JSON.parse(options.body) as unknown,
      });
      return Promise.resolve(
        new Response(
          JSON.stringify({
            ok: true,
            result: method === 'getUpdates' ? [] : true,
          }),
        ),
      );
    });
    const { bot } = setup(false, false);
    await bot.api.getUpdates({
      offset: 42,
      timeout: 30,
      allowed_updates: ['message', 'callback_query'],
    });
    await bot.api.deleteWebhook({ drop_pending_updates: false });
    await bot.api.sendMessage(allowedUserId, 'پیام آزمایشی');
    expect(requests.map((request) => request.url)).toEqual(
      ['getUpdates', 'deleteWebhook', 'sendMessage'].map(
        (method) =>
          `https://tapi.bale.ai/bot123456:${'x'.repeat(35)}/${method}`,
      ),
    );
    expect(requests[0]?.payload).toEqual({ offset: 42, timeout: 30 });
    expect(requests[1]?.payload).toEqual({});
    expect(requests[2]?.payload).toEqual({
      chat_id: allowedUserId,
      text: 'پیام آزمایشی',
    });
  });

  it('handles old-client callbacks without calling unsupported callback answers', async () => {
    const { bot, calls } = setup();
    const update = callback(allowedUserId);
    update.callback_query!.id = '1-old-client';
    await bot.handleUpdate(update);
    expect(calls.map((call) => call.method)).toEqual(['sendMessage']);
    expect(JSON.stringify(calls)).toContain('/help');
  });

  it('falls back to a private message for old-client callback notices', async () => {
    const { bot, calls } = setup();
    bot.callbackQuery('legacy-notice', (ctx) =>
      ctx.answerCallbackQuery({ text: 'اعلان آزمایشی' }),
    );
    const update = callback(allowedUserId, 'legacy-notice');
    update.callback_query!.id = '1-old-client';
    await bot.handleUpdate(update);
    expect(calls).toEqual([
      {
        method: 'sendMessage',
        payload: { chat_id: allowedUserId, text: 'اعلان آزمایشی' },
      },
    ]);
  });

  it('serves accurate global monitoring status only after authorization', async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(command(allowedUserId, '/status'));
    expect(calls).toHaveLength(1);
    expect(JSON.stringify(calls)).toContain('وضعیت پایش بورس');
    expect(JSON.stringify(calls)).toContain('داده معتبر و تازه کافی نیست');
  });
  it('replies to /start with Persian information and a help button', async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(command(allowedUserId));
    expect(calls).toHaveLength(1);
    expect(calls[0]?.method).toBe('sendMessage');
    expect(calls[0]?.payload).toMatchObject({
      chat_id: allowedUserId,
      reply_markup: {
        inline_keyboard: [
          [{ text: 'راهنما', callback_data: 'help' }],
          [{ text: 'تنظیم اعلان‌ها', callback_data: 'preferences' }],
        ],
      },
    });
    expect(calls[0]?.payload).toHaveProperty(
      'text',
      expect.stringContaining('/alert'),
    );
  });

  it('serves help through both the command and inline button', async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(command(allowedUserId, '/help'));
    await bot.handleUpdate(callback(allowedUserId));
    expect(calls.map((call) => call.method)).toEqual([
      'sendMessage',
      'answerCallbackQuery',
      'sendMessage',
    ]);
    expect(calls[2]?.payload).toEqual(calls[0]?.payload);
  });

  it.each(['/start', '/help'])(
    'blocks unauthorized %s before any API call',
    async (text) => {
      const { bot, calls } = setup();
      await bot.handleUpdate(command(999, text));
      expect(calls).toEqual([]);
    },
  );

  it('blocks forged callbacks and messages without a sender', async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(callback(999));
    await bot.handleUpdate({ update_id: 3 });
    expect(calls).toEqual([]);
  });

  it('does not expose private replies in a group, even to the allowed user', async () => {
    const { bot, calls } = setup();
    await bot.handleUpdate(command(allowedUserId, '/start', true));
    expect(calls).toEqual([]);
  });

  it.each([allowedUserId, 987654321, 456789123])(
    'registers an authorized private user %s',
    async (id) => {
      const { bot, users } = setup();
      await bot.handleUpdate(command(id));
      expect(users.preferences(String(id))?.privateChatId).toBe(String(id));
    },
  );

  it('keeps search selections, watchlists, callbacks, and preferences private', async () => {
    const { bot, calls, users, provider } = setup();
    await bot.handleUpdate(command(allowedUserId, '/add خودرو'));
    await bot.handleUpdate(callback(987654321, `add:${instrument.insCode}`));
    expect(users.listSymbols('987654321')).toEqual([]);
    await bot.handleUpdate(
      callback(allowedUserId, `add:${instrument.insCode}`),
    );
    expect(users.listSymbols(String(allowedUserId))).toHaveLength(1);
    await bot.handleUpdate(callback(987654321, `remove:${instrument.insCode}`));
    await bot.handleUpdate(callback(987654321, `queue:${instrument.insCode}`));
    expect(provider.snapshot).not.toHaveBeenCalled();
    expect(users.listSymbols(String(allowedUserId))).toHaveLength(1);
    calls.length = 0;
    await bot.handleUpdate(command(987654321, '/list'));
    expect(JSON.stringify(calls)).not.toContain(instrument.symbol);
    await bot.handleUpdate(callback(987654321, 'notifications:off'));
    expect(users.preferences('987654321')?.notificationsEnabled).toBe(false);
    expect(users.preferences(String(allowedUserId))?.notificationsEnabled).toBe(
      true,
    );
    await bot.handleUpdate(command(987654321));
    expect(users.preferences('987654321')?.notificationsEnabled).toBe(false);
    await bot.handleUpdate(
      callback(allowedUserId, `queue:${instrument.insCode}`),
    );
    expect(provider.snapshot).toHaveBeenCalledOnce();
    await bot.handleUpdate(command(allowedUserId, '/remove خودرو'));
    expect(users.listSymbols(String(allowedUserId))).toEqual([]);
  });

  it('blocks unauthorized market commands before registration or HTTP access', async () => {
    const { bot, calls, provider, users } = setup();
    for (const text of [
      '/add خودرو',
      '/list',
      '/queue خودرو',
      '/remove خودرو',
      '/status',
      '/alert',
      '/alerts',
      '/history',
      '/remove rule 00000000-0000-0000-0000-000000000000',
    ])
      await bot.handleUpdate(command(999, text));
    await bot.handleUpdate(callback(999, `add:${instrument.insCode}`));
    await bot.handleUpdate(callback(allowedUserId, 'help', 987654321));
    expect(calls).toEqual([]);
    expect(provider.search).not.toHaveBeenCalled();
    expect(provider.snapshot).not.toHaveBeenCalled();
    expect(users.preferences(String(allowedUserId))).toBeUndefined();
  });
});

function textMessage(userId: number, text: string) {
  const update = command(userId, text);
  if (update.message) update.message.entities = [];
  return update;
}
function wizardToken(calls: { payload: unknown }[]) {
  const keys = calls.flatMap(
    (call) =>
      (
        call.payload as {
          reply_markup?: { inline_keyboard: { callback_data?: string }[][] };
        }
      ).reply_markup?.inline_keyboard.flat() ?? [],
  );
  const callback = keys.findLast((key) =>
    key.callback_data?.startsWith('aw:'),
  )?.callback_data;
  const token = callback?.split(':')[1];
  if (!token) throw new Error('Wizard keyboard missing');
  return token;
}
describe('Bale alert management', () => {
  it('guides creation, validates Persian inputs, confirms once and isolates callbacks', async () => {
    const { bot, calls, users, alerts, provider } = setup();
    users.register(String(allowedUserId), String(allowedUserId));
    users.addSymbol(String(allowedUserId), instrument);
    await bot.handleUpdate(command(allowedUserId, '/alert'));
    const token = wizardToken(calls);
    await bot.handleUpdate(
      callback(987654321, `aw:${token}:symbol${instrument.insCode}`),
    );
    expect(alerts.list('987654321')).toHaveLength(0);
    for (const action of [`symbol${instrument.insCode}`, 'type0', 'window5'])
      await bot.handleUpdate(callback(allowedUserId, `aw:${token}:${action}`));
    await bot.handleUpdate(textMessage(allowedUserId, '0'));
    await bot.handleUpdate(textMessage(allowedUserId, '۳۰'));
    await bot.handleUpdate(textMessage(allowedUserId, '-1'));
    await bot.handleUpdate(textMessage(allowedUserId, '۱٬۰۰۰٬۰۰۰'));
    expect(alerts.list(String(allowedUserId))).toHaveLength(0);
    await bot.handleUpdate(callback(allowedUserId, `aw:${token}:save`));
    await bot.handleUpdate(callback(allowedUserId, `aw:${token}:save`));
    expect(alerts.list(String(allowedUserId))).toHaveLength(1);
    expect(alerts.list(String(allowedUserId))[0]?.definition).toEqual({
      type: 'BUY_QUEUE_DROP_PERCENT',
      config: {
        thresholdPercent: 30,
        windowMinutes: 5,
        minimumBaselineVolume: 1_000_000,
      },
    });
    expect(provider.snapshot).not.toHaveBeenCalled();
    expect(provider.search).not.toHaveBeenCalled();
  });
  it('enforces ownership for every management callback, then edits/toggles/removes an owned rule', async () => {
    const { bot, calls, users, alerts } = setup();
    users.register(String(allowedUserId), String(allowedUserId));
    users.addSymbol(String(allowedUserId), instrument);
    const id = alerts.create(String(allowedUserId), instrument.insCode, {
      type: 'SELL_QUEUE_DISAPPEARED',
      config: { minimumPreviousVolume: 0 },
    });
    for (const action of [
      'view',
      'on',
      'off',
      'edit',
      'delete',
      'confirmdelete',
    ])
      await bot.handleUpdate(callback(987654321, `ar:${action}:${id}`));
    expect(alerts.get(String(allowedUserId), id)).toMatchObject({
      enabled: true,
      revision: 1,
    });
    await bot.handleUpdate(command(987654321, `/remove rule ${id}`));
    expect(alerts.get(String(allowedUserId), id)).not.toBeNull();
    await bot.handleUpdate(command(allowedUserId, '/alerts'));
    await bot.handleUpdate(callback(allowedUserId, `ar:view:${id}`));
    await bot.handleUpdate(callback(allowedUserId, `ar:off:${id}`));
    expect(alerts.get(String(allowedUserId), id)?.enabled).toBe(false);
    await bot.handleUpdate(callback(allowedUserId, `ar:on:${id}`));
    await bot.handleUpdate(callback(allowedUserId, `ar:edit:${id}`));
    const token = wizardToken(calls);
    await bot.handleUpdate(callback(allowedUserId, `aw:${token}:type2`));
    await bot.handleUpdate(textMessage(allowedUserId, '۰'));
    await bot.handleUpdate(callback(allowedUserId, `aw:${token}:save`));
    expect(alerts.get(String(allowedUserId), id)?.definition).toEqual({
      type: 'BUY_QUEUE_DISAPPEARED',
      config: { minimumPreviousVolume: 0 },
    });
    await bot.handleUpdate(command(allowedUserId, `/remove rule ${id}`));
    expect(alerts.get(String(allowedUserId), id)).toBeNull();
  });
  it('expires drafts, ignores stale edit confirmations and handles an empty watchlist', async () => {
    const { bot, calls, users, alerts } = setup();
    await bot.handleUpdate(command(allowedUserId, '/alert'));
    expect(calls.at(-1)?.payload).toHaveProperty(
      'text',
      expect.stringContaining('/add'),
    );
    users.addSymbol(String(allowedUserId), instrument);
    const id = alerts.create(String(allowedUserId), instrument.insCode, {
      type: 'BUY_QUEUE_DISAPPEARED',
      config: { minimumPreviousVolume: 0 },
    });
    await bot.handleUpdate(callback(allowedUserId, `ar:edit:${id}`));
    const token = wizardToken(calls);
    await bot.handleUpdate(callback(allowedUserId, `aw:${token}:type3`));
    await bot.handleUpdate(textMessage(allowedUserId, '1'));
    alerts.setEnabled(String(allowedUserId), id, false);
    await bot.handleUpdate(callback(allowedUserId, `aw:${token}:save`));
    expect(alerts.get(String(allowedUserId), id)?.definition.type).toBe(
      'BUY_QUEUE_DISAPPEARED',
    );
    const clock = vi.spyOn(Date, 'now');
    try {
      clock.mockReturnValue(1000);
      await bot.handleUpdate(command(allowedUserId, '/alert'));
      const expired = wizardToken(calls);
      clock.mockReturnValue(602_000);
      await bot.handleUpdate(
        callback(allowedUserId, `aw:${expired}:symbol${instrument.insCode}`),
      );
      expect(calls.at(-1)?.payload).toHaveProperty(
        'text',
        expect.stringContaining('منقضی'),
      );
    } finally {
      clock.mockRestore();
    }
  });
  it('shows only the current owner’s last ten historical events', async () => {
    const { bot, calls, users, alerts, db } = setup();
    for (const id of [String(allowedUserId), '987654321']) {
      users.register(id, id);
      users.addSymbol(id, instrument);
    }
    const own = alerts.create(String(allowedUserId), instrument.insCode, {
      type: 'BUY_QUEUE_DISAPPEARED',
      config: { minimumPreviousVolume: 0 },
    });
    const other = alerts.create('987654321', instrument.insCode, {
      type: 'SELL_QUEUE_DISAPPEARED',
      config: { minimumPreviousVolume: 0 },
    });
    db.insert(alertEvents)
      .values(
        Array.from({ length: 12 }, (_, index) => ({
          id: `event-${index}`,
          ruleId: own,
          message: `mine-${index}`,
          createdAt: index,
        })),
      )
      .run();
    db.insert(alertEvents)
      .values({
        id: 'other-event',
        ruleId: other,
        message: 'private-other-event',
        createdAt: 50,
      })
      .run();
    await bot.handleUpdate(command(allowedUserId, '/history'));
    expect(calls.filter((call) => call.method === 'sendMessage')).toHaveLength(
      10,
    );
    expect(JSON.stringify(calls)).not.toContain('private-other-event');
    expect(calls[0]?.payload).toHaveProperty('text', 'mine-11');
  });
});

function adminButton(calls: { payload: unknown }[], label: string) {
  const payload = calls
    .map(
      (call) =>
        call.payload as {
          reply_markup?: {
            inline_keyboard: { text: string; callback_data?: string }[][];
          };
        },
    )
    .findLast((value) => value.reply_markup);
  const button = payload?.reply_markup?.inline_keyboard
    .flat()
    .find((value) => value.text === label)?.callback_data;
  if (!button) throw new Error(`Missing admin button: ${label}`);
  return button;
}
describe('private Bale destination administration', () => {
  it.each(['channel', 'group'] as const)(
    'blocks public/group/non-admin commands and forged %s callbacks',
    async (chatType) => {
      const { bot, calls, channels } = setup(true, true, chatType);
      await bot.handleUpdate(command(allowedUserId, '/admin', true));
      expect(calls).toHaveLength(0);
      await bot.handleUpdate(command(999, '/admin'));
      expect(calls).toHaveLength(0);
      await bot.handleUpdate(command(456789123, '/admin'));
      expect(calls.at(-1)?.payload).toHaveProperty(
        'text',
        expect.stringContaining('دسترسی'),
      );
      await bot.handleUpdate(command(allowedUserId, '/admin'));
      await channels.refresh();
      const id = channels.create(String(allowedUserId), instrument, {
        type: 'GROUP',
        operator: 'AND',
        conditions: [
          {
            type: 'BUY_QUEUE_DISAPPEARED',
            config: { minimumPreviousVolume: 0 },
          },
        ],
      });
      const before = calls.length;
      for (const action of [
        'view',
        'edit',
        'on',
        'off',
        'delete',
        'confirmdelete',
      ])
        await bot.handleUpdate(callback(456789123, `ad:${action}:${id}`));
      await bot.handleUpdate(callback(456789123, 'ad:status'));
      await bot.handleUpdate(callback(456789123, 'af:12345678:save'));
      expect(
        calls
          .slice(before)
          .every((call) => call.method === 'answerCallbackQuery'),
      ).toBe(true);
      expect(channels.get(String(allowedUserId), id)).toMatchObject({
        enabled: true,
        revision: 1,
      });
      expect(JSON.stringify(calls.slice(before))).not.toContain(
        '-1001234567890',
      );
    },
  );
  it.each(['channel', 'group'] as const)(
    'builds, edits, removes and combines %s conditions with Persian validation and saves once',
    async (chatType) => {
      const { bot, calls, channels, provider } = setup(true, true, chatType);
      const press = async (label: string) =>
        bot.handleUpdate(callback(allowedUserId, adminButton(calls, label)));
      await bot.handleUpdate(command(allowedUserId, '/admin'));
      await press('ساخت هشدار جدید');
      await press('انتخاب این مقصد');
      await bot.handleUpdate(textMessage(allowedUserId, 'خودرو'));
      await press('خودرو');
      await press('کاهش صف خرید');
      await bot.handleUpdate(textMessage(allowedUserId, '۰'));
      expect(calls.at(-1)?.payload).toHaveProperty(
        'text',
        expect.stringContaining('نامعتبر'),
      );
      await bot.handleUpdate(textMessage(allowedUserId, '۳۰'));
      await press('۵ دقیقه');
      await bot.handleUpdate(textMessage(allowedUserId, '۰'));
      await press('افزودن شرط');
      await press('حداقل حجم صف خرید');
      await bot.handleUpdate(textMessage(allowedUserId, '۱٬۰۰۰٬۰۰۰'));
      await press('ویرایش شرط ۲');
      await press('ناپدید شدن صف فروش');
      await bot.handleUpdate(textMessage(allowedUserId, '۱۰۰'));
      await press('حذف شرط ۲');
      await press('افزودن شرط');
      await press('حداقل حجم صف خرید');
      await bot.handleUpdate(textMessage(allowedUserId, '۵۰۰'));
      await press('تغییر به: حداقل یکی از شرط‌ها');
      const save = adminButton(calls, 'تأیید و ذخیره');
      await Promise.all([
        bot.handleUpdate(callback(allowedUserId, save)),
        bot.handleUpdate(callback(allowedUserId, save)),
      ]);
      expect(channels.list(String(allowedUserId))).toHaveLength(1);
      expect(channels.list(String(allowedUserId))[0]?.definition).toEqual({
        type: 'GROUP',
        operator: 'OR',
        conditions: [
          {
            type: 'BUY_QUEUE_DROP_PERCENT',
            config: {
              thresholdPercent: 30,
              windowMinutes: 5,
              minimumBaselineVolume: 0,
            },
          },
          { type: 'BUY_QUEUE_VOLUME_ABOVE', config: { minimumVolume: 500 } },
        ],
      });
      expect(provider.snapshot).not.toHaveBeenCalled();
      expect(provider.search).toHaveBeenCalledTimes(1);
      expect(calls.some((call) => call.method === 'editMessageText')).toBe(
        true,
      );
    },
  );
  it('supports pagination, independent toggles, status and confirmed deletion by another configured admin', async () => {
    const { bot, calls, channels } = setup(true);
    await bot.handleUpdate(command(allowedUserId, '/admin'));
    await channels.refresh();
    const ids = Array.from({ length: 11 }, () =>
      channels.create(String(allowedUserId), instrument, {
        type: 'BUY_QUEUE_DISAPPEARED',
        config: { minimumPreviousVolume: 0 },
      }),
    );
    await bot.handleUpdate(command(987654321, '/admin'));
    await bot.handleUpdate(callback(987654321, 'ad:list:0'));
    expect(adminButton(calls, 'بعدی')).toBe('ad:list:1');
    await bot.handleUpdate(callback(987654321, 'ad:list:1'));
    expect(adminButton(calls, 'قبلی')).toBe('ad:list:0');
    const id = ids[0] ?? '';
    await bot.handleUpdate(callback(987654321, `ad:off:${id}`));
    expect(channels.get('987654321', id)?.enabled).toBe(false);
    await bot.handleUpdate(callback(987654321, `ad:on:${id}`));
    expect(channels.get('987654321', id)?.enabled).toBe(true);
    await bot.handleUpdate(callback(987654321, 'ad:status'));
    expect(calls.at(-1)?.payload).toHaveProperty(
      'text',
      expect.stringContaining('-1001234567890'),
    );
    await bot.handleUpdate(callback(987654321, `ad:delete:${id}`));
    expect(channels.list('987654321')).toHaveLength(11);
    await bot.handleUpdate(callback(987654321, `ad:confirmdelete:${id}`));
    expect(channels.list('987654321')).toHaveLength(10);
    expect(channels.get('987654321', ids[1] ?? '')).not.toBeNull();
  });
  it('handles Back/Cancel, stale drafts, permission denial and concurrent admin edits safely', async () => {
    const { bot, calls, channels, setCanPost } = setup(true);
    const press = async (label: string) =>
      bot.handleUpdate(callback(allowedUserId, adminButton(calls, label)));
    await bot.handleUpdate(command(allowedUserId, '/admin'));
    await press('ساخت هشدار جدید');
    await press('انتخاب این مقصد');
    await press('بازگشت');
    expect(adminButton(calls, 'انتخاب این مقصد')).toContain('af:');
    await press('انتخاب این مقصد');
    await bot.handleUpdate(textMessage(allowedUserId, 'خودرو'));
    await press('خودرو');
    await press('ناپدید شدن صف خرید');
    await bot.handleUpdate(textMessage(allowedUserId, '۰'));
    setCanPost(false);
    await press('تأیید و ذخیره');
    expect(channels.list(String(allowedUserId))).toHaveLength(0);
    expect(calls.at(-1)?.payload).toHaveProperty(
      'text',
      expect.stringContaining('اجازه'),
    );
    const stale = adminButton(calls, 'تأیید و ذخیره');
    await press('لغو');
    await bot.handleUpdate(callback(allowedUserId, stale));
    expect(channels.list(String(allowedUserId))).toHaveLength(0);
    setCanPost(true);
    await channels.refresh();
    const id = channels.create(String(allowedUserId), instrument, {
      type: 'BUY_QUEUE_DISAPPEARED',
      config: { minimumPreviousVolume: 0 },
    });
    await bot.handleUpdate(callback(allowedUserId, `ad:edit:${id}`));
    const pendingSave = adminButton(calls, 'تأیید و ذخیره');
    await bot.handleUpdate(command(987654321, '/admin'));
    channels.setEnabled('987654321', id, false);
    await bot.handleUpdate(callback(allowedUserId, pendingSave));
    expect(calls.at(-1)?.payload).toHaveProperty(
      'text',
      expect.stringContaining('تغییر کرده'),
    );
    expect(channels.get(String(allowedUserId), id)?.enabled).toBe(false);
  });
});
