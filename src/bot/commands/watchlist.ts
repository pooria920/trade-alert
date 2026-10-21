import { InlineKeyboard, type Bot, type Context } from 'grammy';
import type { MarketService } from '../../market/market-service.js';
import {
  normalizePersian,
  type Instrument,
} from '../../market/market.types.js';
import type { UserService } from '../../users/user-service.js';

export function registerWatchlistCommands(
  bot: Bot,
  users: UserService,
  market: MarketService,
) {
  const searches = new Map<
    string,
    { expiresAt: number; instruments: Instrument[] }
  >();
  const userId = (ctx: Context) => String(ctx.from?.id);

  async function list(
    ctx: Context,
    page = 0,
    action: 'queue' | 'remove' = 'queue',
  ) {
    const instruments = users.listSymbols(userId(ctx));
    if (!instruments.length) {
      await ctx.reply(
        'فهرست شما خالی است. برای افزودن نماد، از /add همراه نام نماد استفاده کنید.',
      );
      return;
    }
    const current = Number.isSafeInteger(page)
      ? Math.max(0, Math.min(page, Math.ceil(instruments.length / 10) - 1))
      : 0;
    const keyboard = new InlineKeyboard();
    for (const instrument of instruments.slice(
      current * 10,
      current * 10 + 10,
    )) {
      keyboard.text(instrument.symbol, `${action}:${instrument.insCode}`).row();
    }
    if (current > 0) keyboard.text('قبلی', `list:${action}:${current - 1}`);
    if ((current + 1) * 10 < instruments.length)
      keyboard.text('بعدی', `list:${action}:${current + 1}`);
    await ctx.reply(
      action === 'remove'
        ? 'نماد موردنظر را برای حذف انتخاب کنید:'
        : 'نمادهای شما؛ برای بررسی صف، نماد را انتخاب کنید:',
      { reply_markup: keyboard },
    );
  }

  async function queue(ctx: Context, insCode: string) {
    const instrument = users.findSymbol(userId(ctx), insCode);
    if (!instrument) {
      await ctx.reply('این نماد در فهرست شما نیست.');
      return;
    }
    const result = await market.queue({
      ...instrument,
      name: instrument.symbol,
    });
    if (!result.valid) {
      await ctx.reply(
        `نماد: ${instrument.symbol}\nاطلاعات صف قابل تأیید نیست. زمان، کامل بودن سفارش‌ها یا حدود قیمت معتبر نیست؛ حجم صف اعلام نمی‌شود.`,
      );
      return;
    }
    const snapshot = result.snapshot;
    const type =
      snapshot.queueType === 'buy'
        ? 'صف خرید'
        : snapshot.queueType === 'sell'
          ? 'صف فروش'
          : 'بدون صف';
    const time = new Intl.DateTimeFormat('fa-IR', {
      timeZone: 'Asia/Tehran',
      hour: '2-digit',
      minute: '2-digit',
    }).format(snapshot.timestamp);
    await ctx.reply(
      `نماد: ${snapshot.symbol}\nوضعیت: ${type}\nحجم صف: ${snapshot.volume.toLocaleString('fa-IR')}\nتعداد سفارش: ${snapshot.orderCount.toLocaleString('fa-IR')}\nزمان: ${time} به وقت تهران`,
    );
  }

  async function remove(ctx: Context, insCode: string) {
    const removed = users.removeSymbol(userId(ctx), insCode);
    await ctx.reply(
      removed
        ? 'نماد و قواعد مربوط به آن از فهرست شما حذف شد.'
        : 'این نماد در فهرست شما نیست.',
    );
  }

  async function select(ctx: Context, action: 'queue' | 'remove') {
    const query = normalizePersian(
      typeof ctx.match === 'string' ? ctx.match : '',
    );
    const matches = users
      .listSymbols(userId(ctx))
      .filter(
        (instrument) =>
          instrument.insCode === query ||
          normalizePersian(instrument.symbol) === query,
      );
    if (!query || matches.length > 1) {
      await list(ctx, 0, action);
      return;
    }
    const instrument = matches[0];
    if (!instrument) {
      await ctx.reply('این نماد در فهرست شما نیست.');
      return;
    }
    if (action === 'queue') await queue(ctx, instrument.insCode);
    else await remove(ctx, instrument.insCode);
  }

  bot.command('add', async (ctx) => {
    const query = ctx.match.trim();
    if (!query || query.length > 80) {
      await ctx.reply('نام نماد را بعد از دستور بنویسید؛ نمونه: /add خودرو');
      return;
    }
    searches.delete(userId(ctx));
    const instruments = (await market.search(query)).slice(0, 8);
    searches.set(userId(ctx), {
      expiresAt: Date.now() + 5 * 60_000,
      instruments,
    });
    if (!instruments.length) {
      await ctx.reply('نمادی با این نام پیدا نشد.');
      return;
    }
    const keyboard = new InlineKeyboard();
    for (const instrument of instruments)
      keyboard
        .text(
          `${instrument.symbol} (${instrument.insCode})`,
          `add:${instrument.insCode}`,
        )
        .row();
    await ctx.reply(
      'نماد را برای افزودن به فهرست خود انتخاب کنید؛ حداکثر هشت نتیجه نمایش داده می‌شود:',
      { reply_markup: keyboard },
    );
  });
  bot.callbackQuery(/^add:([1-9]\d{0,19})$/, async (ctx) => {
    await ctx.answerCallbackQuery();
    const search = searches.get(userId(ctx));
    const instrument =
      search && search.expiresAt >= Date.now()
        ? search.instruments.find((item) => item.insCode === ctx.match[1])
        : undefined;
    if (!instrument) {
      await ctx.reply(
        'این انتخاب منقضی شده است. جست‌وجو را با /add تکرار کنید.',
      );
      return;
    }
    users.addSymbol(userId(ctx), instrument);
    await ctx.reply(`نماد ${instrument.symbol} به فهرست شما اضافه شد.`);
  });
  bot.command('list', (ctx) => list(ctx));
  bot.command('queue', (ctx) => select(ctx, 'queue'));
  bot.command('remove', (ctx) => select(ctx, 'remove'));
  bot.callbackQuery(/^list:(queue|remove):(\d+)$/, async (ctx) => {
    await ctx.answerCallbackQuery();
    await list(
      ctx,
      Number(ctx.match[2]),
      ctx.match[1] === 'remove' ? 'remove' : 'queue',
    );
  });
  bot.callbackQuery(/^(queue|remove):([1-9]\d{0,19})$/, async (ctx) => {
    await ctx.answerCallbackQuery();
    const insCode = ctx.match[2];
    if (!insCode) return;
    if (ctx.match[1] === 'queue') await queue(ctx, insCode);
    else await remove(ctx, insCode);
  });
}
