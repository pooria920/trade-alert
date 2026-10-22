import { sql } from 'drizzle-orm';
import {
  check,
  index,
  integer,
  primaryKey,
  real,
  sqliteTable,
  text,
  uniqueIndex,
} from 'drizzle-orm/sqlite-core';

// UTC Unix milliseconds throughout; insCode stays TEXT, including in foreign keys.
export const symbols = sqliteTable('symbols', {
  insCode: text('ins_code').primaryKey(),
  symbol: text('symbol').notNull(),
  createdAt: integer('created_at').notNull(),
});

// Historical SQL names are preserved; a fresh Bale database stores only Bale IDs.
export const users = sqliteTable('users', {
  telegramUserId: text('telegram_user_id').primaryKey(),
  privateChatId: text('private_chat_id').notNull(),
  notificationsEnabled: integer('notifications_enabled', { mode: 'boolean' })
    .notNull()
    .default(true),
  createdAt: integer('created_at').notNull(),
  updatedAt: integer('updated_at').notNull(),
});

export const watchlists = sqliteTable(
  'watchlists',
  {
    userId: text('user_id')
      .notNull()
      .references(() => users.telegramUserId, { onDelete: 'cascade' }),
    insCode: text('ins_code')
      .notNull()
      .references(() => symbols.insCode, { onDelete: 'cascade' }),
    createdAt: integer('created_at').notNull(),
  },
  (table) => [
    primaryKey({ columns: [table.userId, table.insCode] }),
    index('watchlists_symbol_idx').on(table.insCode),
  ],
);

export const telegramChannels = sqliteTable('telegram_channels', {
  channelId: text('channel_id').primaryKey(),
  title: text('title').notNull().default('کانال تنظیم‌شده'),
  canPost: integer('can_post', { mode: 'boolean' }).notNull().default(false),
  checkedAt: integer('checked_at'),
  permissionError: text('permission_error'),
});

export const alertRules = sqliteTable(
  'alert_rules',
  {
    id: text('id').primaryKey(),
    // NULL is reserved for quarantined legacy rules; application queries always scope by owner.
    ownerId: text('owner_id').references(() => users.telegramUserId),
    insCode: text('ins_code')
      .notNull()
      .references(() => symbols.insCode, { onDelete: 'cascade' }),
    // Legacy compatibility slot only. New conditions live in definition.type without DB type checks.
    type: text('type', {
      enum: [
        'BUY_QUEUE_DROP_PERCENT',
        'SELL_QUEUE_DROP_PERCENT',
        'BUY_QUEUE_DISAPPEARED',
        'SELL_QUEUE_DISAPPEARED',
      ],
    }).notNull(),
    thresholdPercent: real('threshold_percent'),
    windowMinutes: integer('window_minutes').notNull().default(1),
    minimumBaselineVolume: integer('minimum_baseline_volume')
      .notNull()
      .default(1),
    cooldownSeconds: integer('cooldown_seconds').notNull().default(0),
    enabled: integer('enabled', { mode: 'boolean' }).notNull().default(true),
    armed: integer('armed', { mode: 'boolean' }).notNull().default(true),
    lastTriggeredAt: integer('last_triggered_at'),
    createdAt: integer('created_at').notNull(),
    // Authoritative discriminated config; original columns preserve legacy compatibility.
    definition: text('definition'),
    revision: integer('revision').notNull().default(1),
    channelId: text('channel_id').references(() => telegramChannels.channelId),
  },
  (table) => [
    index('alert_rules_owner_symbol_idx').on(table.ownerId, table.insCode),
    index('alert_rules_enabled_symbol_idx').on(table.enabled, table.insCode),
    index('alert_rules_channel_idx').on(table.channelId, table.enabled),
    check(
      'alert_rule_type',
      sql`${table.type} IN ('BUY_QUEUE_DROP_PERCENT', 'SELL_QUEUE_DROP_PERCENT', 'BUY_QUEUE_DISAPPEARED', 'SELL_QUEUE_DISAPPEARED')`,
    ),
    check(
      'alert_rule_threshold',
      sql`(${table.type} NOT IN ('BUY_QUEUE_DROP_PERCENT', 'SELL_QUEUE_DROP_PERCENT')) OR (${table.thresholdPercent} IS NOT NULL AND ${table.thresholdPercent} > 0 AND ${table.thresholdPercent} <= 100)`,
    ),
    check('alert_rule_window', sql`${table.windowMinutes} IN (1, 5)`),
    check('alert_rule_baseline', sql`${table.minimumBaselineVolume} > 0`),
    check('alert_rule_cooldown', sql`${table.cooldownSeconds} >= 0`),
  ],
);

export const queueSnapshots = sqliteTable(
  'queue_snapshots',
  {
    id: integer('id').primaryKey({ autoIncrement: true }),
    insCode: text('ins_code')
      .notNull()
      .references(() => symbols.insCode, { onDelete: 'cascade' }),
    timestamp: integer('timestamp').notNull(),
    // NULL preserves unknown provenance of pre-monitor history; never backfill receipt times.
    collectedAt: integer('collected_at'),
    symbol: text('symbol'),
    sessionId: text('session_id'),
    queueType: text('queue_type', { enum: ['buy', 'sell', 'none'] }).notNull(),
    volume: integer('volume').notNull(),
    price: real('price'),
    orderCount: integer('order_count').notNull(),
  },
  (table) => [
    uniqueIndex('queue_snapshots_symbol_time_idx').on(
      table.insCode,
      table.timestamp,
    ),
    index('queue_snapshots_retention_idx').on(table.timestamp),
    check(
      'snapshot_queue_type',
      sql`${table.queueType} IN ('buy', 'sell', 'none')`,
    ),
    check(
      'snapshot_quantities',
      sql`${table.volume} >= 0 AND ${table.orderCount} >= 0`,
    ),
    check('snapshot_price', sql`${table.price} IS NULL OR ${table.price} > 0`),
  ],
);

export const alertEvents = sqliteTable(
  'alert_events',
  {
    id: text('id').primaryKey(),
    ruleId: text('rule_id')
      .notNull()
      .references(() => alertRules.id, { onDelete: 'cascade' }),
    message: text('message').notNull(),
    createdAt: integer('created_at').notNull(),
    sentAt: integer('sent_at'),
    occurrenceKey: text('occurrence_key'),
    payload: text('payload'),
    ruleRevision: integer('rule_revision'),
  },
  (table) => [
    index('alert_events_rule_time_idx').on(table.ruleId, table.createdAt),
    uniqueIndex('alert_events_occurrence_idx').on(table.occurrenceKey),
  ],
);

export const alertStates = sqliteTable('alert_states', {
  ruleId: text('rule_id')
    .primaryKey()
    .references(() => alertRules.id, { onDelete: 'cascade' }),
  revision: integer('revision').notNull(),
  sessionId: text('session_id').notNull(),
  status: text('status', {
    enum: ['ARMED', 'TRIGGERED', 'WAITING_FOR_RECOVERY'],
  }).notNull(),
  lastEvaluatedAt: integer('last_evaluated_at').notNull(),
  lastTriggeredAt: integer('last_triggered_at'),
  baselineVolume: integer('baseline_volume'),
  recoveryData: text('recovery_data'),
});

export const notificationOutbox = sqliteTable(
  'notification_outbox',
  {
    eventId: text('event_id')
      .primaryKey()
      .references(() => alertEvents.id, { onDelete: 'cascade' }),
    status: text('status', {
      enum: [
        'PENDING',
        'SENDING',
        'DELIVERED',
        'FAILED',
        'EXPIRED',
        'CANCELLED',
      ],
    })
      .notNull()
      .default('PENDING'),
    attempts: integer('attempts').notNull().default(0),
    nextAttemptAt: integer('next_attempt_at').notNull(),
    leaseUntil: integer('lease_until'),
    leaseToken: text('lease_token'),
    errorCode: text('error_code'),
  },
  (table) => [
    index('notification_outbox_due_idx').on(table.status, table.nextAttemptAt),
  ],
);

export const applicationState = sqliteTable('application_state', {
  key: text('key').primaryKey(),
  value: text('value').notNull(),
  updatedAt: integer('updated_at').notNull(),
});
