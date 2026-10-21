import { expect, it } from 'vitest';
import { formatMonitoringStatus } from '../src/bot/commands/status.command.js';
import { makeStatus, fixedNow } from './fixtures/monitoring.js';

it('does not equate API success with verified fresh market data', () => {
  const text = formatMonitoringStatus(
    makeStatus({ lastSuccessfulFetch: fixedNow }),
    fixedNow,
  );
  expect(text).toContain('داده معتبر و تازه کافی نیست');
  expect(text).toContain('هنوز دریافت نشده');
  expect(text).toContain('۱۰ ثانیه');
  expect(text).toContain('به وقت تهران');
  expect(
    formatMonitoringStatus(
      makeStatus({ lastVerifiedSnapshot: fixedNow }),
      fixedNow,
    ),
  ).toContain('داده معتبر و تازه دریافت شده');
  expect(
    formatMonitoringStatus(
      makeStatus({ lastVerifiedSnapshot: fixedNow - 31_000 }),
      fixedNow,
    ),
  ).toContain('کافی نیست');
  expect(
    formatMonitoringStatus(makeStatus({ isMarketOpen: false }), fixedNow),
  ).toContain('بسته طبق برنامه');
  expect(
    formatMonitoringStatus(makeStatus({ isRunning: false }), fixedNow),
  ).toContain('متوقف');
  expect(
    formatMonitoringStatus(
      makeStatus({ isMarketOpen: false, scheduleConfigured: false }),
      fixedNow,
    ),
  ).toContain('نشست پیکربندی نشده');
});
