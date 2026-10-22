import { randomBytes } from 'node:crypto';
import { InlineKeyboard, type Bot, type Context } from 'grammy';
import type { AlertService } from '../../alerts/alert-service.js';
import {
  eventPayloadSchema,
  type AlertType,
  type RuleDefinition,
} from '../../alerts/alert.types.js';
import {
  alertLabels,
  formatNotification,
} from '../../notifications/notifier.js';
import type { UserService } from '../../users/user-service.js';

type Flow = {
  token: string;
  expiresAt: number;
  step: 'symbol' | 'type' | 'window' | 'threshold' | 'minimum' | 'confirm';
  insCode?: string;
  type?: AlertType;
  window?: 1 | 5;
  threshold?: number;
  minimum?: number;
  editId?: string;
  revision?: number;
};
const types = Object.keys(alertLabels) as AlertType[];
const number = (text: string) =>
  Number(
    text
      .trim()
      .replace(/[۰-۹]/g, (c) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(c)))
      .replace(/[٠-٩]/g, (c) => String('٠١٢٣٤٥٦٧٨٩'.indexOf(c)))
      .replace(/[٬,]/g, '')
      .replace('٫', '.'),
  );
export function definitionText(definition: RuleDefinition) {
  const config = definition.config;
  return `${alertLabels[definition.type]}${'thresholdPercent' in config ? `\nآستانه کاهش: ${config.thresholdPercent.toLocaleString('fa-IR')}٪\nبازه مقایسه: ${config.windowMinutes.toLocaleString('fa-IR')} دقیقه\nحداقل حجم مبنا: ${config.minimumBaselineVolume.toLocaleString('fa-IR')} سهم` : `\nحداقل حجم قبلی: ${config.minimumPreviousVolume.toLocaleString('fa-IR')} سهم`}`;
}
export function registerAlertCommands(
  bot: Bot,
  alerts: AlertService,
  users: UserService,
) {
  // ponytail: one expiring in-memory wizard per authorized user; unfinished drafts are intentionally lost on restart.
  const flows = new Map<string, Flow>();
  const owner = (ctx: Context) => String(ctx.from?.id);
  const keyboard = (flow: Flow, actions: [string, string][]) => {
    const keys = new InlineKeyboard();
    for (const [label, action] of actions)
      keys.text(label, `aw:${flow.token}:${action}`).row();
    return keys.text('لغو', `aw:${flow.token}:cancel`);
  };
  const definition = (flow: Flow): RuleDefinition => {
    const type = flow.type;
    if (!type) throw new Error('Incomplete draft');
    if (type === 'BUY_QUEUE_DROP_PERCENT' || type === 'SELL_QUEUE_DROP_PERCENT')
      return {
        type,
        config: {
          thresholdPercent: flow.threshold ?? 0,
          windowMinutes: flow.window ?? 1,
          minimumBaselineVolume: flow.minimum ?? 0,
        },
      };
    return { type, config: { minimumPreviousVolume: flow.minimum ?? 0 } };
  };
  async function chooseType(ctx: Context, flow: Flow) {
    flow.step = 'type';
    await ctx.reply('نوع هشدار را انتخاب کنید:', {
      reply_markup: keyboard(
        flow,
        types.map((type, index) => [alertLabels[type], `type${index}`]),
      ),
    });
  }
  async function confirm(ctx: Context, flow: Flow) {
    flow.step = 'confirm';
    const symbol = users.findSymbol(owner(ctx), flow.insCode ?? '');
    if (!symbol) {
      flows.delete(owner(ctx));
      await ctx.reply('این نماد در فهرست شما نیست.');
      return;
    }
    await ctx.reply(
      `تأیید هشدار\nنماد: ${symbol.symbol}\n${definitionText(definition(flow))}`,
      { reply_markup: keyboard(flow, [['تأیید و ذخیره', 'save']]) },
    );
  }
  async function symbols(ctx: Context, flow: Flow, page = 0) {
    const watched = users.listSymbols(owner(ctx));
    if (!watched.length) {
      flows.delete(owner(ctx));
      await ctx.reply('ابتدا با /add نماد را به فهرست خود اضافه کنید.');
      return;
    }
    const actions: [string, string][] = watched
      .slice(page * 10, page * 10 + 10)
      .map((symbol) => [symbol.symbol, `symbol${symbol.insCode}`]);
    if (page > 0) actions.push(['قبلی', `page${page - 1}`]);
    if ((page + 1) * 10 < watched.length)
      actions.push(['بعدی', `page${page + 1}`]);
    await ctx.reply('ساخت هشدار جدید؛ نماد را انتخاب کنید:', {
      reply_markup: keyboard(flow, actions),
    });
  }
  bot.command('alert', async (ctx) => {
    const flow: Flow = {
      token: randomBytes(4).toString('hex'),
      expiresAt: Date.now() + 10 * 60_000,
      step: 'symbol',
    };
    flows.set(owner(ctx), flow);
    await symbols(ctx, flow);
  });
  async function list(ctx: Context, page = 0) {
    const rules = alerts.list(owner(ctx));
    if (!rules.length) {
      await ctx.reply('هشداری ندارید. با /alert هشدار بسازید.');
      return;
    }
    const keys = new InlineKeyboard();
    const current = Math.max(
      0,
      Math.min(page, Math.ceil(rules.length / 10) - 1),
    );
    for (const rule of rules.slice(current * 10, current * 10 + 10))
      keys
        .text(
          `${rule.enabled ? 'روشن' : 'خاموش'} · ${users.findSymbol(owner(ctx), rule.insCode)?.symbol ?? rule.insCode} · ${alertLabels[rule.definition.type]}`,
          `ar:view:${rule.id}`,
        )
        .row();
    if (current > 0) keys.text('قبلی', `ar:page:${current - 1}`);
    if ((current + 1) * 10 < rules.length)
      keys.text('بعدی', `ar:page:${current + 1}`);
    await ctx.reply('هشدارهای شما؛ برای مدیریت انتخاب کنید:', {
      reply_markup: keys,
    });
  }
  bot.command('alerts', (ctx) => list(ctx));
  bot.callbackQuery('remove:alerts', async (ctx) => {
    await ctx.answerCallbackQuery();
    await list(ctx);
  });
  // Leave watched-symbol removal to the existing handler.
  bot.command('remove', async (ctx, next) => {
    if (!ctx.match.trim()) {
      await ctx.reply(
        'برای حذف هشدار، آن را در /alerts انتخاب کنید؛ برای حذف نماد، /remove خودرو را بنویسید.',
      );
      await next();
      return;
    }
    const match = /^rule\s+([a-f\d-]{36})$/i.exec(ctx.match.trim());
    if (!match?.[1]) {
      await next();
      return;
    }
    await ctx.reply(
      alerts.remove(owner(ctx), match[1])
        ? 'هشدار حذف شد.'
        : 'این هشدار در فهرست شما نیست.',
    );
  });
  bot.callbackQuery(
    /^ar:(view|on|off|edit|delete|confirmdelete|page):([a-f\d-]+)$/,
    async (ctx) => {
      await ctx.answerCallbackQuery();
      const action = ctx.match[1];
      const id = ctx.match[2] ?? '';
      if (action === 'page') {
        await list(ctx, Number(id));
        return;
      }
      const rule = alerts.get(owner(ctx), id);
      if (!rule) {
        await ctx.reply('این هشدار در فهرست شما نیست.');
        return;
      }
      if (action === 'confirmdelete') {
        alerts.remove(owner(ctx), id);
        await ctx.reply('هشدار حذف شد.');
        return;
      }
      if (action === 'delete') {
        await ctx.reply('این هشدار و سابقهٔ آن حذف شود؟', {
          reply_markup: new InlineKeyboard().text(
            'تأیید حذف',
            `ar:confirmdelete:${id}`,
          ),
        });
        return;
      }
      if (action === 'on' || action === 'off') {
        alerts.setEnabled(owner(ctx), id, action === 'on');
        await ctx.reply('وضعیت هشدار ذخیره شد.');
        return;
      }
      if (action === 'edit') {
        const flow: Flow = {
          token: randomBytes(4).toString('hex'),
          expiresAt: Date.now() + 10 * 60_000,
          step: 'type',
          insCode: rule.insCode,
          editId: rule.id,
          revision: rule.revision,
        };
        flows.set(owner(ctx), flow);
        await chooseType(ctx, flow);
        return;
      }
      await ctx.reply(
        `نماد: ${users.findSymbol(owner(ctx), rule.insCode)?.symbol ?? rule.insCode}\n${definitionText(rule.definition)}\nوضعیت: ${rule.enabled ? 'روشن' : 'خاموش'}\nوقفهٔ هشدار: ${rule.cooldownSeconds.toLocaleString('fa-IR')} ثانیه\nشناسه: ${rule.id}`,
        {
          reply_markup: new InlineKeyboard()
            .text(
              rule.enabled ? 'خاموش کردن' : 'روشن کردن',
              `ar:${rule.enabled ? 'off' : 'on'}:${id}`,
            )
            .row()
            .text('ویرایش تنظیمات', `ar:edit:${id}`)
            .text('حذف', `ar:delete:${id}`),
        },
      );
    },
  );
  bot.callbackQuery(/^aw:([a-f\d]{8}):(.+)$/, async (ctx) => {
    await ctx.answerCallbackQuery();
    const flow = flows.get(owner(ctx));
    if (!flow || flow.token !== ctx.match[1] || flow.expiresAt < Date.now()) {
      if (flow && flow.expiresAt < Date.now()) flows.delete(owner(ctx));
      await ctx.reply('این انتخاب منقضی شده است. با /alert دوباره شروع کنید.');
      return;
    }
    const action = ctx.match[2] ?? '';
    if (action === 'cancel') {
      flows.delete(owner(ctx));
      await ctx.reply('ساخت هشدار لغو شد.');
      return;
    }
    if (flow.step === 'symbol' && /^page\d+$/.test(action)) {
      await symbols(ctx, flow, Number(action.slice(4)));
      return;
    }
    if (flow.step === 'symbol' && /^symbol\d+$/.test(action)) {
      const code = action.slice(6);
      if (!users.findSymbol(owner(ctx), code)) {
        await ctx.reply('این نماد در فهرست شما نیست.');
        return;
      }
      flow.insCode = code;
      await chooseType(ctx, flow);
      return;
    }
    if (flow.step === 'type' && /^type[0-3]$/.test(action)) {
      const selected = types[Number(action.slice(4))];
      if (!selected) return;
      flow.type = selected;
      if (flow.type?.endsWith('DROP_PERCENT')) {
        flow.step = 'window';
        await ctx.reply('بازه مقایسه را انتخاب کنید:', {
          reply_markup: keyboard(flow, [
            ['۱ دقیقه', 'window1'],
            ['۵ دقیقه', 'window5'],
          ]),
        });
      } else {
        flow.step = 'minimum';
        await ctx.reply(
          'حداقل حجم قبلی صف را بنویسید؛ برای نداشتن حداقل، صفر بفرستید.',
        );
      }
      return;
    }
    if (flow.step === 'window' && /^window[15]$/.test(action)) {
      flow.window = action === 'window5' ? 5 : 1;
      flow.step = 'threshold';
      await ctx.reply(
        'درصد کاهش را بنویسید؛ بزرگ‌تر از صفر تا ۱۰۰، با حداکثر شش رقم اعشار.',
      );
      return;
    }
    if (flow.step === 'confirm' && action === 'save') {
      if (flow.editId) {
        const existing = alerts.get(owner(ctx), flow.editId);
        if (!existing || existing.revision !== flow.revision) {
          flows.delete(owner(ctx));
          await ctx.reply(
            'هشدار تغییر کرده یا حذف شده است. از /alerts دوباره انتخاب کنید.',
          );
          return;
        }
        alerts.update(owner(ctx), flow.editId, definition(flow));
      } else alerts.create(owner(ctx), flow.insCode ?? '', definition(flow));
      flows.delete(owner(ctx));
      await ctx.reply(
        'هشدار ذخیره شد. ارزیابی فقط با دادهٔ تازه و قابل تأیید انجام می‌شود.',
      );
      return;
    }
    await ctx.reply('این مرحله تغییر کرده است. از آخرین پیام استفاده کنید.');
  });
  bot.command('history', async (ctx) => {
    const events = alerts.history(owner(ctx));
    if (!events.length) {
      await ctx.reply('هنوز هشداری ثبت نشده است.');
      return;
    }
    const texts = events.map((event) => {
      try {
        const payload = eventPayloadSchema.parse(
          JSON.parse(event.payload ?? 'null') as unknown,
        );
        return formatNotification(payload, event.createdAt, Infinity);
      } catch {
        return event.message || 'هشدار قدیمی';
      }
    });
    // Separate messages avoid Bale's length limit without truncating triggering values.
    for (const text of texts) await ctx.reply(text);
  });
  bot.on('message:text', async (ctx, next) => {
    const flow = flows.get(owner(ctx));
    if (ctx.message.text.startsWith('/')) {
      flows.delete(owner(ctx));
      await next();
      return;
    }
    if (!flow) {
      await next();
      return;
    }
    if (flow.expiresAt < Date.now()) {
      flows.delete(owner(ctx));
      await ctx.reply(
        'فرایند ساخت هشدار منقضی شد. با /alert دوباره شروع کنید.',
      );
      return;
    }
    const value = number(ctx.message.text);
    if (flow.step === 'threshold') {
      if (
        !Number.isFinite(value) ||
        value <= 0 ||
        value > 100 ||
        Number(value.toFixed(6)) !== value
      ) {
        await ctx.reply(
          'درصد باید بزرگ‌تر از صفر تا ۱۰۰ باشد؛ حداکثر شش رقم اعشار.',
        );
        return;
      }
      flow.threshold = value;
      flow.step = 'minimum';
      await ctx.reply(
        'حداقل حجم مبنا را بنویسید؛ برای نداشتن حداقل، صفر بفرستید.',
      );
      return;
    }
    if (flow.step === 'minimum') {
      if (
        !ctx.message.text.trim() ||
        !Number.isSafeInteger(value) ||
        value < 0
      ) {
        await ctx.reply('حجم باید عدد صحیح و نامنفی باشد.');
        return;
      }
      flow.minimum = value;
      await confirm(ctx, flow);
      return;
    }
    await ctx.reply('از دکمه‌های آخرین پیام استفاده کنید.');
  });
}
