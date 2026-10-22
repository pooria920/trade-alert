import { randomBytes } from 'node:crypto';
import { InlineKeyboard, type Bot, type Context } from 'grammy';
import type { ChannelService } from '../../alerts/channel-service.js';
import {
  conditionSchema,
  type RuleCondition,
  type RuleGroup,
} from '../../alerts/composite.js';
import type { MarketService } from '../../market/market-service.js';
import type { Instrument } from '../../market/market.types.js';
import {
  conditionLabels,
  treeText,
  tehranTime,
} from '../../notifications/notifier.js';

type Step =
  | 'channel'
  | 'search'
  | 'results'
  | 'type'
  | 'percent'
  | 'window'
  | 'minimum'
  | 'builder';
type Draft = {
  token: string;
  expires: number;
  step: Step;
  operator: 'AND' | 'OR';
  conditions: RuleCondition[];
  instrument?: Instrument;
  results?: Instrument[];
  selectedType?: RuleCondition['type'];
  percent?: number;
  window?: 1 | 5;
  editIndex?: number;
  ruleId?: string;
  revision?: number;
  saving?: boolean;
};
const types = Object.keys(conditionLabels) as RuleCondition['type'][];
const numeric = (text: string) =>
  Number(
    text
      .trim()
      .replace(/[۰-۹]/g, (c) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(c)))
      .replace(/[٠-٩]/g, (c) => String('٠١٢٣٤٥٦٧٨٩'.indexOf(c)))
      .replace(/[,٬]/g, '')
      .replace('٫', '.'),
  );
export function registerAdminCommands(
  bot: Bot,
  channels: ChannelService,
  market: MarketService,
) {
  // ponytail: bounded in-memory drafts expire in ten minutes; save is atomic, restart discards unfinished work.
  const drafts = new Map<string, Draft>();
  const actor = (ctx: Context) => String(ctx.from?.id);
  const keys = (rows: [string, string][]) => {
    const keyboard = new InlineKeyboard();
    for (const [label, data] of rows) keyboard.text(label, data).row();
    return keyboard;
  };
  async function render(ctx: Context, text: string, keyboard: InlineKeyboard) {
    if (ctx.callbackQuery?.message) {
      try {
        await ctx.editMessageText(text, { reply_markup: keyboard });
        return;
      } catch (error) {
        if (
          error instanceof Error &&
          error.message.includes('message is not modified')
        )
          return;
      }
    }
    await ctx.reply(text, { reply_markup: keyboard });
  }
  async function menu(ctx: Context) {
    drafts.delete(actor(ctx));
    await render(
      ctx,
      'مدیریت هشدارهای کانال',
      keys([
        ['ساخت هشدار جدید', 'ad:new'],
        ['مشاهده هشدارها', 'ad:list:0'],
        ['ویرایش هشدار', 'ad:list:0'],
        ['فعال / غیرفعال کردن هشدار', 'ad:list:0'],
        ['حذف هشدار', 'ad:list:0'],
        ['وضعیت کانال', 'ad:status'],
      ]),
    );
  }
  const tree = (draft: Draft): RuleGroup => ({
    type: 'GROUP',
    operator: draft.operator,
    conditions: draft.conditions,
  });
  async function show(ctx: Context, draft: Draft, notice = '') {
    draft.token = randomBytes(4).toString('hex');
    const rows: [string, string][] = [];
    const action = (label: string, value: string) =>
      rows.push([label, `af:${draft.token}:${value}`]);
    let text = '';
    if (draft.step === 'channel') {
      text = `هشدار جدید کانال\nکانال: ${channels.status(actor(ctx)).title ?? 'کانال تنظیم‌شده'}`;
      action('انتخاب این کانال', 'channel');
    }
    if (draft.step === 'search')
      text = 'نام نماد را برای جست‌وجو بنویسید؛ حداکثر ۸۰ نویسه.';
    if (draft.step === 'results') {
      text = 'نماد را انتخاب کنید:';
      draft.results?.forEach((item, index) =>
        action(item.symbol, `symbol${index}`),
      );
    }
    if (draft.step === 'type') {
      text = 'شرط را انتخاب کنید:';
      types.forEach((type, index) =>
        action(conditionLabels[type], `type${index}`),
      );
    }
    if (draft.step === 'percent')
      text =
        'درصد کاهش را بنویسید؛ بزرگ‌تر از صفر تا ۱۰۰، حداکثر شش رقم اعشار.';
    if (draft.step === 'window') {
      text = 'بازه مقایسه را انتخاب کنید:';
      action('۱ دقیقه', 'window1');
      action('۵ دقیقه', 'window5');
    }
    if (draft.step === 'minimum')
      text = draft.selectedType?.endsWith('VOLUME_ABOVE')
        ? 'حداقل حجم فعلی صف را بنویسید؛ عدد صحیح و بزرگ‌تر از صفر.'
        : 'حداقل حجم مبنا یا حجم قبلی صف را بنویسید؛ برای نداشتن حداقل، صفر بفرستید.';
    if (draft.step === 'builder') {
      text = `هشدار کانال\nکانال: ${channels.status(actor(ctx)).title ?? 'کانال تنظیم‌شده'}\nنماد: ${draft.instrument?.symbol ?? ''}\n\n${treeText(tree(draft))}\n\nبا برقرار شدن شرط‌ها، یک هشدار در کانال منتشر می‌شود.`;
      draft.conditions.forEach((_, index) => {
        action(
          `ویرایش شرط ${(index + 1).toLocaleString('fa-IR')}`,
          `edit${index}`,
        );
        action(
          `حذف شرط ${(index + 1).toLocaleString('fa-IR')}`,
          `remove${index}`,
        );
      });
      if (draft.conditions.length < 8) action('افزودن شرط', 'add');
      action(
        draft.operator === 'AND'
          ? 'تغییر به: حداقل یکی از شرط‌ها'
          : 'تغییر به: همه شرط‌ها',
        'operator',
      );
      if (draft.conditions.length) action('تأیید و ذخیره', 'save');
    }
    action('بازگشت', 'back');
    action('لغو', 'cancel');
    await render(ctx, `${notice ? `${notice}\n\n` : ''}${text}`, keys(rows));
  }
  async function list(ctx: Context, page: number) {
    const rules = channels.list(actor(ctx));
    const current = Number.isSafeInteger(page)
      ? Math.max(
          0,
          Math.min(page, Math.max(0, Math.ceil(rules.length / 10) - 1)),
        )
      : 0;
    const rows: [string, string][] = rules
      .slice(current * 10, current * 10 + 10)
      .map((rule) => [
        `${rule.enabled ? 'روشن' : 'خاموش'} · ${rule.symbol} · ${rule.definition.type === 'GROUP' ? 'هشدار ترکیبی' : conditionLabels[rule.definition.type]}`,
        `ad:view:${rule.id}`,
      ]);
    if (current > 0) rows.push(['قبلی', `ad:list:${current - 1}`]);
    if ((current + 1) * 10 < rules.length)
      rows.push(['بعدی', `ad:list:${current + 1}`]);
    rows.push(['بازگشت', 'ad:menu']);
    await render(
      ctx,
      rules.length
        ? 'هشدارهای کانال؛ برای مدیریت انتخاب کنید:'
        : 'هشداری برای این کانال ثبت نشده است.',
      keys(rows),
    );
  }
  async function status(ctx: Context, check = true) {
    let verified = true;
    if (check) {
      try {
        await channels.refresh(AbortSignal.timeout(10_000));
      } catch {
        verified = false;
        /* Display unverified status; private bot remains available. */
      }
    }
    const value = channels.status(actor(ctx));
    await render(
      ctx,
      `وضعیت کانال\nعنوان: ${value.title ?? 'نامشخص'}\nشناسه کانال: ${value.channelId}\nاجازهٔ ارسال: ${verified && value.canPost ? 'دارد' : 'ندارد یا قابل تأیید نیست'}\nتعداد هشدارها: ${value.total.toLocaleString('fa-IR')}\nفعال: ${value.active.toLocaleString('fa-IR')}\nآخرین ارسال موفق: ${value.lastSuccess === null ? 'ثبت نشده' : tehranTime(value.lastSuccess)}\nآخرین خطای ارسال: ${value.lastError ? `${value.lastError.code} · ${tehranTime(value.lastError.timestamp)}` : 'ثبت نشده'}`,
      keys([
        ['بررسی دوباره', 'ad:status'],
        ['بازگشت', 'ad:menu'],
      ]),
    );
  }
  bot.command('admin', async (ctx) => {
    if (!channels.isAdmin(actor(ctx))) {
      await ctx.reply('دسترسی مدیریت کانال ندارید.');
      return;
    }
    channels.authorize(actor(ctx));
    await menu(ctx);
  });
  bot.callbackQuery(/^ad:(.+)$/, async (ctx) => {
    if (!channels.isAdmin(actor(ctx))) {
      await ctx.answerCallbackQuery({ text: 'دسترسی ندارید.' });
      return;
    }
    channels.authorize(actor(ctx));
    await ctx.answerCallbackQuery();
    const action = ctx.match[1] ?? '';
    if (action === 'menu') {
      await menu(ctx);
      return;
    }
    if (action === 'status') {
      await status(ctx);
      return;
    }
    if (action === 'new') {
      const draft: Draft = {
        token: '',
        expires: Date.now() + 600_000,
        step: 'channel',
        operator: 'AND',
        conditions: [],
      };
      drafts.set(actor(ctx), draft);
      await show(ctx, draft);
      return;
    }
    if (/^list:\d+$/.test(action)) {
      await list(ctx, Number(action.slice(5)));
      return;
    }
    const match =
      /^(view|edit|on|off|delete|confirmdelete):([a-f\d-]{36})$/.exec(action);
    if (!match?.[2]) return;
    const id = match[2];
    const rule = channels.get(actor(ctx), id);
    if (!rule) {
      await render(
        ctx,
        'هشدار پیدا نشد یا به این کانال تعلق ندارد.',
        keys([['بازگشت', 'ad:list:0']]),
      );
      return;
    }
    if (match[1] === 'delete') {
      await render(
        ctx,
        `هشدار ${rule.symbol} و سابقهٔ آن حذف شود؟`,
        keys([
          ['تأیید حذف', `ad:confirmdelete:${id}`],
          ['بازگشت', `ad:view:${id}`],
          ['لغو', 'ad:menu'],
        ]),
      );
      return;
    }
    if (match[1] === 'confirmdelete') {
      channels.remove(actor(ctx), id);
      await list(ctx, 0);
      return;
    }
    if (match[1] === 'on' || match[1] === 'off') {
      if (match[1] === 'on') {
        try {
          if (!(await channels.refresh(AbortSignal.timeout(10_000)))) {
            await status(ctx, false);
            return;
          }
        } catch {
          await status(ctx, false);
          return;
        }
      }
      channels.setEnabled(actor(ctx), id, match[1] === 'on');
      await list(ctx, 0);
      return;
    }
    if (match[1] === 'edit') {
      const conditions =
        rule.definition.type === 'GROUP'
          ? rule.definition.conditions
          : [rule.definition];
      if (conditions.some((condition) => condition.type === 'GROUP')) {
        await render(
          ctx,
          'ویرایش گروه‌های تودرتو در این رابط پشتیبانی نمی‌شود. مشاهده، تغییر وضعیت و حذف همچنان ممکن است.',
          keys([['بازگشت', `ad:view:${id}`]]),
        );
        return;
      }
      const draft: Draft = {
        token: '',
        expires: Date.now() + 600_000,
        step: 'builder',
        operator:
          rule.definition.type === 'GROUP' ? rule.definition.operator : 'AND',
        conditions: conditions as RuleCondition[],
        instrument: {
          insCode: rule.insCode,
          symbol: rule.symbol,
          name: rule.symbol,
        },
        ruleId: rule.id,
        revision: rule.revision,
      };
      drafts.set(actor(ctx), draft);
      await show(ctx, draft);
      return;
    }
    await render(
      ctx,
      `نماد: ${rule.symbol}\nوضعیت: ${rule.enabled ? 'روشن' : 'خاموش'}\n${treeText(rule.definition)}`,
      keys([
        ['ویرایش', `ad:edit:${id}`],
        [
          rule.enabled ? 'غیرفعال کردن' : 'فعال کردن',
          `ad:${rule.enabled ? 'off' : 'on'}:${id}`,
        ],
        ['حذف', `ad:delete:${id}`],
        ['بازگشت', 'ad:list:0'],
        ['لغو', 'ad:menu'],
      ]),
    );
  });
  bot.callbackQuery(/^af:([a-f\d]{8}):(.+)$/, async (ctx) => {
    if (!channels.isAdmin(actor(ctx))) {
      await ctx.answerCallbackQuery({ text: 'دسترسی ندارید.' });
      return;
    }
    channels.authorize(actor(ctx));
    await ctx.answerCallbackQuery();
    const draft = drafts.get(actor(ctx));
    if (!draft || draft.token !== ctx.match[1] || Date.now() > draft.expires) {
      if (draft && Date.now() > draft.expires) drafts.delete(actor(ctx));
      await render(
        ctx,
        'این مرحله منقضی یا تغییر کرده است. از آخرین پیام یا /admin استفاده کنید.',
        keys([['مدیریت', 'ad:menu']]),
      );
      return;
    }
    const action = ctx.match[2] ?? '';
    if (draft.saving) return;
    if (action === 'cancel') {
      await menu(ctx);
      return;
    }
    if (action === 'back') {
      const back: Record<Step, Step> = {
        channel: 'channel',
        search: 'channel',
        results: 'search',
        type: draft.conditions.length ? 'builder' : 'search',
        percent: 'type',
        window: 'percent',
        minimum: draft.selectedType?.endsWith('DROP_PERCENT')
          ? 'window'
          : 'type',
        builder: 'search',
      };
      if (draft.step === 'channel') {
        await menu(ctx);
        return;
      }
      draft.step = back[draft.step];
      await show(ctx, draft);
      return;
    }
    if (draft.step === 'channel' && action === 'channel') draft.step = 'search';
    else if (draft.step === 'results' && /^symbol\d+$/.test(action)) {
      const instrument = draft.results?.[Number(action.slice(6))];
      if (!instrument) return;
      if (draft.ruleId && draft.instrument?.insCode !== instrument.insCode) {
        await show(ctx, draft, 'برای تغییر نماد، هشدار جدید بسازید.');
        return;
      }
      draft.instrument = instrument;
      draft.step = draft.conditions.length ? 'builder' : 'type';
    } else if (draft.step === 'type' && /^type\d+$/.test(action)) {
      const type = types[Number(action.slice(4))];
      if (!type) return;
      draft.selectedType = type;
      delete draft.percent;
      delete draft.window;
      draft.step = type.endsWith('DROP_PERCENT') ? 'percent' : 'minimum';
    } else if (draft.step === 'window' && /^window[15]$/.test(action)) {
      draft.window = action === 'window1' ? 1 : 5;
      draft.step = 'minimum';
    } else if (draft.step === 'builder' && action === 'operator')
      draft.operator = draft.operator === 'AND' ? 'OR' : 'AND';
    else if (
      draft.step === 'builder' &&
      action === 'add' &&
      draft.conditions.length < 8
    ) {
      delete draft.editIndex;
      draft.step = 'type';
    } else if (draft.step === 'builder' && /^edit\d+$/.test(action)) {
      const index = Number(action.slice(4));
      if (!draft.conditions[index]) return;
      draft.editIndex = index;
      draft.step = 'type';
    } else if (draft.step === 'builder' && /^remove\d+$/.test(action)) {
      draft.conditions.splice(Number(action.slice(6)), 1);
      delete draft.editIndex;
    } else if (
      draft.step === 'builder' &&
      action === 'save' &&
      draft.instrument &&
      draft.conditions.length
    ) {
      draft.saving = true;
      try {
        if (!(await channels.refresh(AbortSignal.timeout(10_000)))) {
          draft.saving = false;
          if (drafts.get(actor(ctx)) !== draft) return;
          await show(
            ctx,
            draft,
            'ربات اجازهٔ ارسال در کانال ندارد. دسترسی را اصلاح و دوباره تلاش کنید.',
          );
          return;
        }
      } catch {
        draft.saving = false;
        if (drafts.get(actor(ctx)) !== draft) return;
        await show(
          ctx,
          draft,
          'دسترسی کانال قابل تأیید نیست. کمی بعد دوباره تلاش کنید.',
        );
        return;
      }
      if (drafts.get(actor(ctx)) !== draft) return;
      if (draft.ruleId) {
        if (
          !channels.update(
            actor(ctx),
            draft.ruleId,
            tree(draft),
            undefined,
            draft.revision,
          )
        ) {
          drafts.delete(actor(ctx));
          await render(
            ctx,
            'هشدار تغییر کرده یا حذف شده است؛ دوباره انتخاب کنید.',
            keys([['بازگشت', 'ad:list:0']]),
          );
          return;
        }
      } else channels.create(actor(ctx), draft.instrument, tree(draft));
      drafts.delete(actor(ctx));
      await render(
        ctx,
        'هشدار کانال ذخیره شد. انتشار فقط با دادهٔ تازه و قابل تأیید انجام می‌شود.',
        keys([
          ['مشاهده هشدارها', 'ad:list:0'],
          ['بازگشت', 'ad:menu'],
        ]),
      );
      return;
    } else return;
    await show(ctx, draft);
  });
  bot.on('message:text', async (ctx, next) => {
    const draft = drafts.get(actor(ctx));
    if (!draft) {
      await next();
      return;
    }
    if (ctx.message.text.startsWith('/')) {
      drafts.delete(actor(ctx));
      await next();
      return;
    }
    channels.authorize(actor(ctx));
    if (draft.saving) return;
    if (Date.now() > draft.expires) {
      drafts.delete(actor(ctx));
      await ctx.reply('فرایند منقضی شد. با /admin دوباره شروع کنید.');
      return;
    }
    if (draft.step === 'search') {
      const query = ctx.message.text.trim();
      if (!query || query.length > 80) {
        await show(ctx, draft, 'نام نماد باید بین ۱ تا ۸۰ نویسه باشد.');
        return;
      }
      draft.results = (await market.search(query)).slice(0, 8);
      draft.step = draft.results.length ? 'results' : 'search';
      await show(
        ctx,
        draft,
        draft.results.length ? '' : 'نمادی پیدا نشد. دوباره جست‌وجو کنید.',
      );
      return;
    }
    const value = numeric(ctx.message.text);
    if (draft.step === 'percent') {
      if (
        !Number.isFinite(value) ||
        value <= 0 ||
        value > 100 ||
        Number(value.toFixed(6)) !== value
      ) {
        await show(
          ctx,
          draft,
          'درصد نامعتبر است؛ عددی بزرگ‌تر از صفر تا ۱۰۰ بنویسید.',
        );
        return;
      }
      draft.percent = value;
      draft.step = 'window';
      await show(ctx, draft);
      return;
    }
    if (draft.step === 'minimum') {
      if (
        !ctx.message.text.trim() ||
        !Number.isSafeInteger(value) ||
        value < 0 ||
        (draft.selectedType?.endsWith('VOLUME_ABOVE') && value === 0)
      ) {
        await show(
          ctx,
          draft,
          'حجم نامعتبر است. یک عدد صحیح در محدودهٔ خواسته‌شده بنویسید.',
        );
        return;
      }
      const type = draft.selectedType;
      const config = type?.endsWith('DROP_PERCENT')
        ? {
            thresholdPercent: draft.percent,
            windowMinutes: draft.window,
            minimumBaselineVolume: value,
          }
        : type?.endsWith('VOLUME_ABOVE')
          ? { minimumVolume: value }
          : { minimumPreviousVolume: value };
      const condition = conditionSchema.safeParse({ type, config });
      if (!condition.success) {
        draft.step = 'type';
        await show(ctx, draft, 'تنظیمات کامل نیست؛ شرط را دوباره انتخاب کنید.');
        return;
      }
      if (draft.editIndex !== undefined)
        draft.conditions[draft.editIndex] = condition.data;
      else if (draft.conditions.length < 8)
        draft.conditions.push(condition.data);
      delete draft.editIndex;
      draft.step = 'builder';
      await show(ctx, draft);
      return;
    }
    await show(ctx, draft, 'از دکمه‌های آخرین پیام استفاده کنید.');
  });
}
