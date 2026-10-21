import { describe, expect, it } from 'vitest';
import {
  MarketSchedule,
  sessionsSchema,
} from '../src/market/market-schedule.js';
import { parseEnv } from '../src/config/env.js';

// Explicit test schedule, not a statement of real exchange hours or holidays.
const sessions = sessionsSchema.parse([
  { market: 'equities', days: [1, 2, 3, 6, 7], start: '09:00', end: '12:30' },
]);
const at = (date: string) => Date.parse(date);
describe('explicit Tehran market sessions', () => {
  it('identifies separate intraday sessions and Tehran calendar dates', () => {
    const schedule = new MarketSchedule(
      sessionsSchema.parse([
        { days: [3, 4], start: '09:00', end: '10:00' },
        { days: [3, 4], start: '11:00', end: '12:00' },
      ]),
    );
    const morning = schedule.sessionId(at('2026-10-21T05:30:00Z'));
    expect(morning).toBe('equities:2026-10-21:09:00:10:00');
    expect(schedule.sessionId(at('2026-10-21T06:30:00Z'))).toBeNull();
    expect(schedule.sessionId(at('2026-10-21T07:30:00Z'))).not.toBe(morning);
    expect(schedule.sessionId(at('2026-10-22T05:30:00Z'))).not.toBe(morning);
  });
  it('uses Tehran time and exclusive end boundaries rather than the host timezone', () => {
    const schedule = new MarketSchedule(sessions);
    expect(schedule.isOpen(at('2026-10-21T05:29:59Z'))).toBe(false);
    expect(schedule.isOpen(at('2026-10-21T05:30:00Z'))).toBe(true);
    expect(schedule.isOpen(at('2026-10-21T08:59:59Z'))).toBe(true);
    expect(schedule.isOpen(at('2026-10-21T09:00:00Z'))).toBe(false);
    expect(schedule.isOpen(at('2026-10-22T06:30:00Z'))).toBe(false);
    expect(schedule.isOpen(at('2026-10-23T06:30:00Z'))).toBe(false);
    expect(schedule.isOpen(at('2026-10-24T06:30:00Z'))).toBe(true);
  });
  it('supports explicit holidays/closures, profiles, safe defaults and a testing override', () => {
    expect(
      new MarketSchedule(sessions, ['2026-10-21']).isOpen(
        at('2026-10-21T06:30:00Z'),
      ),
    ).toBe(false);
    expect(
      new MarketSchedule(sessions, [], false, 'other').isOpen(
        at('2026-10-21T06:30:00Z'),
      ),
    ).toBe(false);
    expect(new MarketSchedule([]).configured).toBe(false);
    expect(new MarketSchedule([]).isOpen(at('2026-10-21T06:30:00Z'))).toBe(
      false,
    );
    expect(
      new MarketSchedule([], ['2026-10-21'], true).isOpen(
        at('2026-10-21T06:30:00Z'),
      ),
    ).toBe(true);
  });
  it('validates new configuration and defaults force-active off', () => {
    const base = {
      TELEGRAM_BOT_TOKEN: `123456:${'x'.repeat(35)}`,
      TELEGRAM_ALLOWED_USER_IDS: '123',
    };
    expect(parseEnv(base).MARKET_MONITOR_FORCE_ACTIVE).toBe(false);
    expect(parseEnv(base).MARKET_SESSIONS_JSON).toEqual([]);
    for (const invalid of [
      { MARKET_MONITOR_FORCE_ACTIVE: 'yes' },
      { MARKET_MAX_CONCURRENT_REQUESTS: '0' },
      { MARKET_REQUEST_TIMEOUT_MS: '0' },
      { MARKET_SESSIONS_JSON: 'bad' },
      { MARKET_SESSIONS_JSON: '[{"days":[9],"start":"09:00","end":"12:30"}]' },
      { MARKET_SESSIONS_JSON: '[{"days":[3],"start":"13:00","end":"12:00"}]' },
      { MARKET_CLOSED_DATES: '2026-02-30' },
      { SNAPSHOT_CLEANUP_INTERVAL_MS: '0' },
    ])
      expect(() => parseEnv({ ...base, ...invalid })).toThrow(
        'Invalid environment',
      );
  });
});
