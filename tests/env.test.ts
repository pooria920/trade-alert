import { describe, expect, it } from 'vitest';
import { parseEnv } from '../src/config/env.js';

const required = {
  TELEGRAM_BOT_TOKEN: `123456:${'x'.repeat(35)}`,
  TELEGRAM_ALLOWED_USER_IDS: '123456789, 987654321,456789123',
};

describe('environment validation', () => {
  it('validates and converts configuration with safe defaults', () => {
    const env = parseEnv(required);
    expect(env.TELEGRAM_ALLOWED_USER_IDS).toEqual([
      '123456789',
      '987654321',
      '456789123',
    ]);
    expect(env.MARKET_POLL_INTERVAL_MS).toBe(10_000);
    expect(env.DATABASE_URL).toBe('./data/stock-alert.db');
    expect(env.SNAPSHOT_RETENTION_HOURS).toBe(24);
    expect(env).toMatchObject({
      ALERT_DEFAULT_COOLDOWN_SECONDS: 300,
      ALERT_HISTORY_LIMIT: 10,
      NOTIFICATION_MAX_RETRIES: 5,
      NOTIFICATION_RETRY_BASE_MS: 2000,
      NOTIFICATION_MAX_AGE_MINUTES: 15,
    });
  });

  it.each([
    { TELEGRAM_BOT_TOKEN: '' },
    ...[
      '',
      '1.5',
      '9007199254740992',
      '123,',
      '123,,456',
      '123,-456',
      'abc',
      '123,0',
      '00123',
    ].map((value) => ({ TELEGRAM_ALLOWED_USER_IDS: value })),
    { TSETMC_BASE_URL: 'http://example.com/api' },
    { TSETMC_BASE_URL: 'not-a-url' },
    { TSETMC_REQUEST_TIMEOUT_MS: '0' },
    { MARKET_POLL_INTERVAL_MS: '1' },
    { SNAPSHOT_RETENTION_HOURS: '-1' },
    { DATABASE_URL: '' },
    { DATABASE_URL: 'https://example.com/db' },
    { LOG_LEVEL: 'everything' },
    { ALERT_DEFAULT_COOLDOWN_SECONDS: '-1' },
    { ALERT_HISTORY_LIMIT: '11' },
    { NOTIFICATION_MAX_RETRIES: '-1' },
    { NOTIFICATION_RETRY_BASE_MS: '0' },
    { NOTIFICATION_MAX_AGE_MINUTES: '0' },
  ])('rejects invalid values: %o', (invalid) => {
    expect(() => parseEnv({ ...required, ...invalid })).toThrow(
      'Invalid environment variables',
    );
  });

  it('deduplicates IDs without imposing a user count limit', () => {
    expect(
      parseEnv({ ...required, TELEGRAM_ALLOWED_USER_IDS: '1,2,3,4,1' })
        .TELEGRAM_ALLOWED_USER_IDS,
    ).toEqual(['1', '2', '3', '4']);
    expect(() =>
      parseEnv({ ...required, TELEGRAM_ALLOWED_USER_ID: '123' }),
    ).toThrow('Replace TELEGRAM_ALLOWED_USER_ID');
  });

  it('reports invalid field names without exposing credentials', () => {
    const secret = 'a-secret-that-is-not-a-token';
    expect(() => parseEnv({ ...required, TELEGRAM_BOT_TOKEN: secret })).toThrow(
      'TELEGRAM_BOT_TOKEN',
    );
    try {
      parseEnv({ ...required, TELEGRAM_BOT_TOKEN: secret });
    } catch (error) {
      expect(String(error)).not.toContain(secret);
    }
  });
});
