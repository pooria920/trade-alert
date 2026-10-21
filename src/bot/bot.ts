import { Bot, InlineKeyboard } from 'grammy';
import type { Logger } from 'pino';
import type { Env } from '../config/env.js';
import { showHelp } from './commands/help.js';
import { authorize } from './middleware/authorize.js';
import { registerWatchlistCommands } from './commands/watchlist.js';
import type { UserService } from '../users/user-service.js';
import type { MarketService } from '../market/market-service.js';
import type { MarketMonitor } from '../market/market-monitor.js';
import { showMonitoringStatus } from './commands/status.command.js';
import type { AlertService } from '../alerts/alert-service.js';
import { registerAlertCommands } from './commands/alerts.js';
import type { ChannelService } from '../alerts/channel-service.js';
import { registerAdminCommands } from './commands/admin.js';

export function createBot(
  env: Env,
  logger: Logger,
  services: {
    users: UserService;
    market: MarketService;
    monitor: Pick<MarketMonitor, 'getStatus'>;
    alerts: AlertService;
    channels?: ChannelService;
  },
) {
  const bot = new Bot(env.TELEGRAM_BOT_TOKEN);
  bot.use(
    authorize([
      ...env.TELEGRAM_ALLOWED_USER_IDS,
      ...env.TELEGRAM_ADMIN_USER_IDS,
    ]),
  );
  bot.use(async (ctx, next) => {
    if (!ctx.from || !ctx.chat) return;
    try {
      services.users.register(String(ctx.from.id), String(ctx.chat.id));
      await next();
    } catch (err) {
      logger.error({ err, updateId: ctx.update.update_id }, 'Command failed');
      await ctx.reply(
        'دریافت اطلاعات یا اجرای دستور ناموفق بود. کمی بعد دوباره تلاش کنید.',
      );
    }
  });

  bot.command('start', async (ctx) => {
    await ctx.reply(
      'ربات صف بورس آماده است. فهرست نمادها و هشدارهای هر کاربر جداست.\nساخت هشدار: /alert\nمدیریت هشدارها: /alerts\nبررسی صف: /queue\nوضعیت پایش: /status\nهشدار فقط با دادهٔ تازه و قابل تأیید صادر می‌شود.',
      {
        reply_markup: new InlineKeyboard()
          .text('راهنما', 'help')
          .row()
          .text('تنظیم اعلان‌ها', 'preferences'),
      },
    );
  });
  bot.command('help', showHelp);
  bot.command('status', (ctx) =>
    showMonitoringStatus(ctx, () => services.monitor.getStatus()),
  );
  bot.callbackQuery('help', async (ctx) => {
    await ctx.answerCallbackQuery();
    await showHelp(ctx);
  });
  async function preferences(ctx: import('grammy').Context) {
    const enabled =
      services.users.preferences(String(ctx.from?.id))?.notificationsEnabled ??
      false;
    await ctx.reply(`اعلان‌های حساب شما: ${enabled ? 'روشن' : 'خاموش'}`, {
      reply_markup: new InlineKeyboard().text(
        enabled ? 'خاموش کردن' : 'روشن کردن',
        `notifications:${enabled ? 'off' : 'on'}`,
      ),
    });
  }
  bot.callbackQuery('preferences', async (ctx) => {
    await ctx.answerCallbackQuery();
    await preferences(ctx);
  });
  bot.callbackQuery(/^notifications:(on|off)$/, async (ctx) => {
    await ctx.answerCallbackQuery();
    services.users.setNotifications(String(ctx.from.id), ctx.match[1] === 'on');
    await preferences(ctx);
  });
  if (services.channels)
    registerAdminCommands(bot, services.channels, services.market);
  registerAlertCommands(bot, services.alerts, services.users);
  registerWatchlistCommands(bot, services.users, services.market);

  bot.catch((error) => {
    logger.error(
      { err: error.error, updateId: error.ctx.update.update_id },
      'Telegram update failed',
    );
  });
  return bot;
}
