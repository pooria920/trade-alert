import type { Context } from 'grammy';
import type { MonitoringStatus } from '../../monitoring/monitoring-status.js';

export function formatMonitoringStatus(
  status: Readonly<MonitoringStatus>,
  now = Date.now(),
): string {
  const timestamp = (value: number | null) =>
    value === null
      ? 'هنوز دریافت نشده'
      : new Intl.DateTimeFormat('fa-IR', {
          timeZone: 'Asia/Tehran',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        }).format(value);
  const fresh =
    status.lastVerifiedSnapshot !== null &&
    now - status.lastVerifiedSnapshot >= 0 &&
    now - status.lastVerifiedSnapshot <= status.maxAgeMs;
  const service = !status.isRunning
    ? 'متوقف'
    : !status.isMarketOpen
      ? 'در انتظار نشست'
      : !status.monitoredSymbols
        ? 'بدون نماد'
        : !fresh ||
            status.freshSymbols !== status.monitoredSymbols ||
            status.consecutiveErrors
          ? 'فعال؛ داده معتبر و تازه کافی نیست'
          : 'فعال؛ داده معتبر و تازه دریافت شده';
  const market = status.forceActive
    ? 'اجباری برای آزمایش'
    : !status.scheduleConfigured
      ? 'نشست پیکربندی نشده'
      : status.isMarketOpen
        ? 'باز طبق برنامه تنظیم‌شده'
        : 'بسته طبق برنامه تنظیم‌شده';
  return `وضعیت پایش بورس\n\nوضعیت سرویس: ${service}\nوضعیت بازار: ${market}\nتعداد نمادهای تحت نظر: ${status.monitoredSymbols.toLocaleString('fa-IR')}\nنماد با داده معتبر و تازه: ${status.freshSymbols.toLocaleString('fa-IR')}\nفاصله بررسی: ${(status.pollIntervalMs / 1000).toLocaleString('fa-IR')} ثانیه\nآخرین چرخه: ${timestamp(status.lastPollingCycle)}\nآخرین دریافت موفق API: ${timestamp(status.lastSuccessfulFetch)}\nآخرین زمان داده معتبر و تازه: ${timestamp(status.lastVerifiedSnapshot)}\nخطاهای متوالی چرخه‌ها: ${status.consecutiveErrors.toLocaleString('fa-IR')}${status.rateLimitedUntil > now ? '\nدریافت موقتاً به‌علت محدودیت نرخ متوقف است.' : ''}\nزمان‌ها به وقت تهران است.\nدریافت موفق API به‌تنهایی به معنی سلامت داده بازار نیست.`;
}

export async function showMonitoringStatus(
  ctx: Context,
  getStatus: () => Readonly<MonitoringStatus>,
) {
  await ctx.reply(formatMonitoringStatus(getStatus()));
}
