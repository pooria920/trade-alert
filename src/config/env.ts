import { z } from 'zod';
import { resolve, sep } from 'node:path';
import {
  calendarDateSchema,
  sessionsSchema,
} from '../market/market-schedule.js';

const json: z.ZodType<unknown, string> = z.string().transform((value, ctx) => {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    ctx.addIssue({ code: 'custom', message: 'Invalid JSON' });
    return z.NEVER;
  }
});

const positiveInteger = z.coerce
  .number()
  .int()
  .positive()
  .max(Number.MAX_SAFE_INTEGER);

export const baleUserIdSchema = z
  .string()
  .regex(/^[1-9]\d{0,15}$/)
  .refine(
    (id) => /^[1-9]\d{0,15}$/.test(id) && BigInt(id) <= (1n << 52n) - 1n,
    'Bale IDs must fit in 52 bits',
  );

const allowedUserIdsSchema = z
  .string()
  .transform((value) => value.split(',').map((id) => id.trim()))
  .pipe(z.array(baleUserIdSchema).min(1))
  .transform((ids) => [...new Set(ids)]);
export const channelIdSchema = z
  .string()
  .regex(/^-?[1-9]\d{0,15}$/)
  .refine(
    (id) =>
      /^-?[1-9]\d{0,15}$/.test(id) &&
      BigInt(id) >= -((1n << 52n) - 1n) &&
      BigInt(id) <= (1n << 52n) - 1n,
  );
const adminIdsSchema = z
  .string()
  .default('')
  .transform((value) =>
    value.trim() ? value.split(',').map((id) => id.trim()) : [],
  )
  .pipe(z.array(baleUserIdSchema))
  .transform((ids) => [...new Set(ids)]);

export const databasePathSchema = z
  .string()
  .trim()
  .min(1)
  .refine((path) => !path.includes('\0') && !path.includes('://'), {
    message: 'Use a local SQLite file path, not a URL',
  })
  .default('./data/stock-alert.db');

const envSchema = z
  .object({
    NODE_ENV: z
      .enum(['development', 'test', 'production'])
      .default('development'),
    BALE_BOT_TOKEN: z
      .string()
      .trim()
      .regex(/^\d+:[A-Za-z0-9_-]{30,}$/, 'Invalid bot token format'),
    BALE_ALLOWED_USER_IDS: allowedUserIdsSchema,
    BALE_ADMIN_USER_IDS: adminIdsSchema,
    BALE_CHANNEL_ID: z
      .string()
      .default('')
      .pipe(z.union([z.literal(''), channelIdSchema])),
    TSETMC_BASE_URL: z
      .url({ protocol: /^https$/ })
      .default('https://cdn.tsetmc.com/api'),
    TSETMC_REQUEST_TIMEOUT_MS: positiveInteger.max(120_000).default(5_000),
    TSETMC_MAX_RETRIES: z.coerce.number().int().min(0).max(3).default(2),
    MARKET_DATA_MAX_AGE_MS: positiveInteger.max(300_000).default(30_000),
    MARKET_POLL_INTERVAL_MS: positiveInteger.min(1_000).default(10_000),
    MARKET_MAX_CONCURRENT_REQUESTS: positiveInteger.max(10).default(3),
    MARKET_REQUEST_TIMEOUT_MS: positiveInteger.max(120_000).default(5_000),
    MARKET_MONITOR_FORCE_ACTIVE: z
      .enum(['true', 'false'])
      .default('false')
      .transform((value) => value === 'true'),
    MARKET_SESSIONS_JSON: json.pipe(sessionsSchema).default([]),
    MARKET_CLOSED_DATES: z
      .string()
      .default('')
      .transform((value) =>
        value.trim() ? value.split(',').map((date) => date.trim()) : [],
      )
      .pipe(z.array(calendarDateSchema)),
    MARKET_SESSION_MARKET: z.string().trim().min(1).max(50).default('equities'),
    SNAPSHOT_HISTORY_TOLERANCE_SECONDS: positiveInteger.max(300).default(20),
    SNAPSHOT_CLEANUP_INTERVAL_MS: positiveInteger
      .min(60_000)
      .max(86_400_000)
      .default(900_000),
    SNAPSHOT_RETENTION_HOURS: positiveInteger.max(8_760).default(24),
    ALERT_DEFAULT_COOLDOWN_SECONDS: z.coerce
      .number()
      .int()
      .min(0)
      .max(86_400)
      .default(300),
    ALERT_HISTORY_LIMIT: positiveInteger.max(10).default(10),
    NOTIFICATION_MAX_RETRIES: z.coerce.number().int().min(0).max(10).default(5),
    NOTIFICATION_RETRY_BASE_MS: positiveInteger.max(300_000).default(2000),
    NOTIFICATION_MAX_AGE_MINUTES: positiveInteger.max(1440).default(15),
    DATABASE_URL: databasePathSchema,
    LOG_LEVEL: z
      .enum(['fatal', 'error', 'warn', 'info', 'debug', 'trace', 'silent'])
      .default('info'),
  })
  .refine(
    (env) =>
      Boolean(env.BALE_CHANNEL_ID) === Boolean(env.BALE_ADMIN_USER_IDS.length),
    {
      path: ['BALE_CHANNEL_ID'],
      message: 'Configure channel and admins together',
    },
  );

export type Env = z.infer<typeof envSchema>;

export function parseEnv(input: NodeJS.ProcessEnv): Env {
  if (
    Object.keys(input).some(
      (key) => key.startsWith('TELEGRAM_') && input[key] !== undefined,
    )
  )
    throw new Error(
      'Replace TELEGRAM_* settings with BALE_* settings; Telegram identities cannot be reused',
    );
  if (input.BALE_ALLOWED_USER_ID !== undefined) {
    throw new Error('Replace BALE_ALLOWED_USER_ID with BALE_ALLOWED_USER_IDS');
  }
  const result = envSchema.safeParse(input);
  if (!result.success) {
    // Report field names only: validation errors must never echo secret input.
    throw new Error(
      `Invalid environment variables: ${[...new Set(result.error.issues.map((issue) => issue.path.join('.')))].join(', ')}`,
    );
  }
  if (
    result.data.BALE_ADMIN_USER_IDS.some(
      (id) => !result.data.BALE_ALLOWED_USER_IDS.includes(id),
    )
  )
    throw new Error(
      'BALE_ADMIN_USER_IDS must be authorized in BALE_ALLOWED_USER_IDS',
    );
  if (
    result.data.NODE_ENV === 'production' &&
    (result.data.MARKET_MONITOR_FORCE_ACTIVE ||
      result.data.DATABASE_URL === ':memory:' ||
      input.DEBUG?.trim())
  )
    throw new Error(
      'Production forbids schedule bypass, in-memory databases and DEBUG request logging',
    );
  if (
    input.STOCK_ALERT_CONTAINER === 'true' &&
    !resolve(result.data.DATABASE_URL).startsWith(
      `${resolve('/app/data')}${sep}`,
    )
  )
    throw new Error(
      'Container DATABASE_URL must remain below /app/data for persistent storage',
    );
  return result.data;
}
