import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { and, asc, eq, isNull, lte, or, sql } from 'drizzle-orm';
import type { Logger } from 'pino';
import type { openDatabase } from '../database/client.js';
import {
  alertEvents,
  alertRules,
  applicationState,
  notificationOutbox,
} from '../database/schema.js';
import type { UserService } from '../users/user-service.js';
import { eventPayloadSchema } from '../alerts/alert.types.js';
import { compositeEventPayloadSchema } from '../alerts/composite.js';
import type { ChannelService } from '../alerts/channel-service.js';
import { z } from 'zod';
import { formatNotification } from './notifier.js';

export interface DeliveryOptions {
  maxRetries: number;
  retryBaseMs: number;
  maxAgeMs: number;
  marketMaxAgeMs: number;
}
export type SendNotification = (
  chatId: string,
  text: string,
  signal: AbortSignal,
) => Promise<unknown>;
function deliveryError(error: unknown) {
  const result =
    typeof error === 'object' && error !== null
      ? (error as {
          error_code?: unknown;
          parameters?: { retry_after?: unknown };
        })
      : {};
  const code = typeof result.error_code === 'number' ? result.error_code : null;
  const seconds = result.parameters?.retry_after;
  const retryAfter =
    typeof seconds === 'number' &&
    Number.isFinite(seconds) &&
    seconds >= 0 &&
    seconds <= Number.MAX_SAFE_INTEGER / 1000
      ? seconds * 1000
      : null;
  return {
    code,
    retryAfter,
    permanent: code !== null && code >= 400 && code < 500 && code !== 429,
  };
}

export class NotificationDispatcher {
  private task: Promise<void> | null = null;
  private abort = new AbortController();
  private active: Promise<void> | null = null;
  constructor(
    private readonly db: ReturnType<typeof openDatabase>['db'],
    private readonly users: UserService,
    private readonly send: SendNotification,
    private readonly options: DeliveryOptions,
    private readonly logger: Logger,
    private readonly now = Date.now,
    private readonly channels?: ChannelService,
  ) {}
  start() {
    if (this.task) return;
    if (this.abort.signal.aborted) this.abort = new AbortController();
    this.task = this.loop().finally(() => {
      this.task = null;
    });
  }
  async stop() {
    this.abort.abort();
    await this.task;
    await this.active;
  }
  private async loop() {
    while (!this.abort.signal.aborted) {
      try {
        await this.dispatchOnce();
      } catch (err) {
        this.logger.error({ err }, 'Notification worker failed');
      }
      try {
        await delay(1000, undefined, { signal: this.abort.signal });
      } catch {
        if (!this.abort.signal.aborted)
          throw new Error('Notification timer failed');
      }
    }
  }
  dispatchOnce(): Promise<void> {
    if (this.active) return this.active;
    this.active = this.deliver().finally(() => {
      this.active = null;
    });
    return this.active;
  }
  private async deliver() {
    if (this.abort.signal.aborted) return;
    const now = this.now();
    const token = randomUUID();
    const item = this.db.transaction(
      (tx) => {
        const cooldown = tx
          .select()
          .from(applicationState)
          .where(eq(applicationState.key, 'bale_retry_until'))
          .get();
        if (cooldown && Number(cooldown.value) > now) return null;
        const row = tx
          .select({
            outbox: notificationOutbox,
            event: alertEvents,
            rule: alertRules,
          })
          .from(notificationOutbox)
          .innerJoin(
            alertEvents,
            eq(alertEvents.id, notificationOutbox.eventId),
          )
          .innerJoin(alertRules, eq(alertRules.id, alertEvents.ruleId))
          .where(
            or(
              and(
                eq(notificationOutbox.status, 'PENDING'),
                lte(notificationOutbox.nextAttemptAt, now),
              ),
              and(
                eq(notificationOutbox.status, 'SENDING'),
                or(
                  isNull(notificationOutbox.leaseUntil),
                  lte(notificationOutbox.leaseUntil, now),
                ),
              ),
            ),
          )
          .orderBy(asc(notificationOutbox.nextAttemptAt))
          .limit(1)
          .get();
        if (!row) return null;
        tx.update(notificationOutbox)
          .set({
            status: 'SENDING',
            attempts: row.outbox.attempts + 1,
            leaseToken: token,
            leaseUntil: now + 60_000,
          })
          .where(eq(notificationOutbox.eventId, row.event.id))
          .run();
        return { ...row, attempts: row.outbox.attempts + 1 };
      },
      { behavior: 'immediate' },
    );
    if (!item) return;
    const finish = (
      status: 'DELIVERED' | 'FAILED' | 'EXPIRED' | 'CANCELLED' | 'PENDING',
      errorCode: string | null = null,
      nextAttemptAt = now,
    ) => {
      this.db.transaction((tx) => {
        const changed = tx
          .update(notificationOutbox)
          .set({
            status,
            errorCode,
            nextAttemptAt,
            leaseToken: null,
            leaseUntil: null,
          })
          .where(
            and(
              eq(notificationOutbox.eventId, item.event.id),
              eq(notificationOutbox.leaseToken, token),
            ),
          )
          .run().changes;
        if (changed && status === 'DELIVERED') {
          tx.update(alertEvents)
            .set({ sentAt: this.now() })
            .where(eq(alertEvents.id, item.event.id))
            .run();
          if (item.rule.channelId !== null) {
            const key = `channel_last_success:${item.rule.channelId}`;
            tx.insert(applicationState)
              .values({ key, value: String(this.now()), updatedAt: this.now() })
              .onConflictDoUpdate({
                target: applicationState.key,
                set: { value: String(this.now()), updatedAt: this.now() },
              })
              .run();
          }
        }
      });
    };
    let payload;
    try {
      payload = z
        .union([eventPayloadSchema, compositeEventPayloadSchema])
        .parse(JSON.parse(item.event.payload ?? 'null') as unknown);
    } catch {
      finish('FAILED', 'INVALID_PAYLOAD');
      return;
    }
    if (
      now - payload.timestamp > this.options.maxAgeMs ||
      now < payload.timestamp
    ) {
      finish('EXPIRED');
      return;
    }
    let target: string | null = null;
    try {
      if (
        item.rule.ownerId &&
        item.rule.enabled &&
        item.rule.revision === item.event.ruleRevision
      )
        target =
          item.rule.channelId === null
            ? this.users.findSymbol(item.rule.ownerId, item.rule.insCode)
              ? this.users.notificationTarget(item.rule.ownerId, item.rule.id)
              : null
            : this.channels?.eligible(item.rule)
              ? item.rule.channelId
              : null;
    } catch {
      /* Removed authorization cancels queued delivery. */
    }
    if (!target) {
      finish('CANCELLED');
      return;
    }
    if (item.attempts > this.options.maxRetries + 1) {
      finish('FAILED', 'RETRY_EXHAUSTED');
      return;
    }
    // A finite API deadline is shorter than the persisted claim lease.
    const deadline = new AbortController();
    const signal = AbortSignal.any([deadline.signal, this.abort.signal]);
    const timeout = setTimeout(() => deadline.abort(), 10_000);
    let onAbort: (() => void) | undefined;
    try {
      await Promise.race([
        (async () => {
          if (item.rule.channelId !== null) {
            if (!this.channels || !(await this.channels.refresh(signal)))
              throw Object.assign(
                new Error('Channel posting permission denied'),
                { error_code: 403 },
              );
            const current = this.db
              .select()
              .from(alertRules)
              .where(eq(alertRules.id, item.rule.id))
              .get();
            if (
              !current ||
              !current.enabled ||
              current.revision !== item.event.ruleRevision ||
              !this.channels.eligible(current)
            ) {
              finish('CANCELLED');
              return;
            }
          }
          signal.throwIfAborted();
          if (
            this.now() - payload.timestamp > this.options.maxAgeMs ||
            this.now() < payload.timestamp
          ) {
            finish('EXPIRED');
            return;
          }
          await this.send(
            target,
            formatNotification(
              payload,
              this.now(),
              this.options.marketMaxAgeMs,
            ),
            signal,
          );
        })(),
        new Promise<never>((_, reject) => {
          onAbort = () => reject(new Error('Notification request interrupted'));
          signal.addEventListener('abort', onAbort, { once: true });
          if (signal.aborted) onAbort();
        }),
      ]);
      finish('DELIVERED');
    } catch (err) {
      const failure = deliveryError(err);
      if (item.rule.channelId !== null)
        this.channels?.recordError(
          failure.code === null ? 'TRANSPORT' : `BALE_${failure.code}`,
          this.now(),
        );
      const backoff = Math.max(
        failure.retryAfter ?? 0,
        Math.min(
          300_000,
          this.options.retryBaseMs * 2 ** Math.min(item.attempts - 1, 20),
        ),
      );
      const retryAt = this.now() + backoff;
      if (failure.code === 429)
        this.db
          .insert(applicationState)
          .values({
            key: 'bale_retry_until',
            value: String(retryAt),
            updatedAt: this.now(),
          })
          .onConflictDoUpdate({
            target: applicationState.key,
            set: {
              value: sql`CAST(MAX(CAST(${applicationState.value} AS INTEGER), ${retryAt}) AS TEXT)`,
              updatedAt: this.now(),
            },
          })
          .run();
      finish(
        failure.permanent || item.attempts > this.options.maxRetries
          ? 'FAILED'
          : 'PENDING',
        failure.code === null ? 'TRANSPORT' : `BALE_${failure.code}`,
        retryAt,
      );
      this.logger.warn(
        { code: failure.code, attempts: item.attempts },
        'Notification delivery failed',
      );
    } finally {
      clearTimeout(timeout);
      if (onAbort) signal.removeEventListener('abort', onAbort);
    }
  }
}
