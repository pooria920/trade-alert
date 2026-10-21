import { randomUUID } from 'node:crypto';
import { and, asc, eq, inArray } from 'drizzle-orm';
import { z } from 'zod';
import type { openDatabase } from '../database/client.js';
import {
  alertEvents,
  alertRules,
  alertStates,
  applicationState,
  notificationOutbox,
  symbols,
  telegramChannels,
} from '../database/schema.js';
import { channelIdSchema } from '../config/env.js';
import { instrumentSchema, type Instrument } from '../market/market.types.js';
import type { UserService } from '../users/user-service.js';
import {
  channelDefinitionSchema,
  parseChannelDefinition,
  type RuleTree,
} from './composite.js';

export type ChannelVerification = (
  channelId: string,
  signal?: AbortSignal,
) => Promise<{ channelId: string; title: string; canPost: boolean }>;
export class ChannelService {
  constructor(
    private readonly db: ReturnType<typeof openDatabase>['db'],
    private readonly users: UserService,
    private readonly admins: readonly string[],
    readonly channelId: string,
    private readonly verify: ChannelVerification,
    private readonly cooldown = 300,
    private readonly now = Date.now,
  ) {
    if (channelId) {
      channelIdSchema.parse(channelId);
      this.db
        .insert(telegramChannels)
        .values({ channelId })
        .onConflictDoNothing()
        .run();
    }
  }
  isAdmin(actor: string) {
    return this.admins.includes(actor);
  }
  authorize(actor: string) {
    if (
      !this.isAdmin(actor) ||
      !this.channelId ||
      this.users.preferences(actor)?.privateChatId !== actor
    )
      throw new Error('Channel administration is not authorized');
  }
  eligible(
    rule: Pick<typeof alertRules.$inferSelect, 'channelId' | 'ownerId'>,
  ) {
    return (
      !!this.channelId &&
      rule.channelId === this.channelId &&
      rule.ownerId !== null &&
      this.isAdmin(rule.ownerId)
    );
  }
  async refresh(signal?: AbortSignal) {
    if (!this.channelId) return false;
    signal?.throwIfAborted();
    try {
      const result = z
        .object({
          channelId: channelIdSchema,
          title: z.string().min(1).max(255),
          canPost: z.boolean(),
        })
        .parse(await this.verify(this.channelId, signal));
      signal?.throwIfAborted();
      if (result.channelId !== this.channelId)
        throw new Error('Unexpected channel');
      this.db
        .update(telegramChannels)
        .set({
          title: result.title,
          canPost: result.canPost,
          checkedAt: this.now(),
          permissionError: result.canPost ? null : 'CANNOT_POST',
        })
        .where(eq(telegramChannels.channelId, this.channelId))
        .run();
      return result.canPost;
    } catch (error) {
      if (signal?.aborted) throw error;
      this.db
        .update(telegramChannels)
        .set({
          canPost: false,
          checkedAt: this.now(),
          permissionError: 'UNAVAILABLE',
        })
        .where(eq(telegramChannels.channelId, this.channelId))
        .run();
      throw error;
    }
  }
  private writable() {
    const channel = this.db
      .select()
      .from(telegramChannels)
      .where(eq(telegramChannels.channelId, this.channelId))
      .get();
    if (
      !channel?.canPost ||
      channel.checkedAt === null ||
      this.now() - channel.checkedAt > 60_000 ||
      this.now() < channel.checkedAt
    )
      throw new Error('Channel posting permission must be verified');
  }
  list(actor: string) {
    this.authorize(actor);
    return this.db
      .select({ rule: alertRules, symbol: symbols.symbol })
      .from(alertRules)
      .innerJoin(symbols, eq(symbols.insCode, alertRules.insCode))
      .where(eq(alertRules.channelId, this.channelId))
      .orderBy(asc(alertRules.createdAt), asc(alertRules.id))
      .all()
      .flatMap(({ rule, symbol }) => {
        const definition = parseChannelDefinition(rule.definition);
        return definition ? [{ ...rule, definition, symbol }] : [];
      });
  }
  get(actor: string, id: string) {
    return this.list(actor).find((rule) => rule.id === id) ?? null;
  }
  create(actor: string, instrument: Instrument, input: RuleTree) {
    this.authorize(actor);
    this.writable();
    const item = instrumentSchema.parse(instrument);
    const definition = channelDefinitionSchema.parse(input);
    const id = randomUUID();
    this.db.transaction((tx) => {
      tx.insert(symbols)
        .values({
          insCode: item.insCode,
          symbol: item.symbol,
          createdAt: this.now(),
        })
        .onConflictDoNothing()
        .run();
      tx.insert(alertRules)
        .values({
          id,
          ownerId: actor,
          channelId: this.channelId,
          insCode: item.insCode,
          type: 'BUY_QUEUE_DISAPPEARED',
          definition: JSON.stringify(definition),
          cooldownSeconds: this.cooldown,
          createdAt: this.now(),
        })
        .run();
    });
    return id;
  }
  update(
    actor: string,
    id: string,
    input: RuleTree,
    enabled?: boolean,
    revision?: number,
  ) {
    this.authorize(actor);
    const definition = channelDefinitionSchema.parse(input);
    const rule = this.get(actor, id);
    if (!rule || (revision !== undefined && rule.revision !== revision))
      return false;
    if (enabled !== false) this.writable();
    this.db.transaction((tx) => {
      tx.update(alertRules)
        .set({
          ownerId: actor,
          definition: JSON.stringify(definition),
          enabled: enabled ?? rule.enabled,
          revision: rule.revision + 1,
          createdAt: this.now(),
        })
        .where(
          and(eq(alertRules.id, id), eq(alertRules.channelId, this.channelId)),
        )
        .run();
      tx.delete(alertStates).where(eq(alertStates.ruleId, id)).run();
      tx.update(notificationOutbox)
        .set({ status: 'CANCELLED' })
        .where(
          and(
            eq(notificationOutbox.status, 'PENDING'),
            inArray(
              notificationOutbox.eventId,
              tx
                .select({ id: alertEvents.id })
                .from(alertEvents)
                .where(eq(alertEvents.ruleId, id)),
            ),
          ),
        )
        .run();
    });
    return true;
  }
  setEnabled(actor: string, id: string, enabled: boolean) {
    const rule = this.get(actor, id);
    return rule
      ? this.update(actor, id, rule.definition, enabled, rule.revision)
      : false;
  }
  remove(actor: string, id: string) {
    this.authorize(actor);
    return (
      this.db
        .delete(alertRules)
        .where(
          and(eq(alertRules.id, id), eq(alertRules.channelId, this.channelId)),
        )
        .run().changes > 0
    );
  }
  monitoredSymbols(): Instrument[] {
    if (!this.channelId) return [];
    return this.db
      .selectDistinct({ insCode: symbols.insCode, symbol: symbols.symbol })
      .from(alertRules)
      .innerJoin(symbols, eq(symbols.insCode, alertRules.insCode))
      .where(
        and(
          eq(alertRules.channelId, this.channelId),
          eq(alertRules.enabled, true),
          inArray(alertRules.ownerId, [...this.admins]),
        ),
      )
      .all()
      .map((item) => ({ ...item, name: item.symbol }));
  }
  recordError(code: string, timestamp: number) {
    const key = `channel_delivery_error:${this.channelId}`;
    const value = JSON.stringify({ code, timestamp });
    this.db
      .insert(applicationState)
      .values({ key, value, updatedAt: timestamp })
      .onConflictDoUpdate({
        target: applicationState.key,
        set: { value, updatedAt: timestamp },
      })
      .run();
  }
  status(actor: string) {
    this.authorize(actor);
    const channel = this.db
      .select()
      .from(telegramChannels)
      .where(eq(telegramChannels.channelId, this.channelId))
      .get();
    const rules = this.list(actor);
    const last = this.db
      .select()
      .from(applicationState)
      .where(eq(applicationState.key, `channel_last_success:${this.channelId}`))
      .get();
    const lastSuccess = last ? Number(last.value) : null;
    const error = this.db
      .select()
      .from(applicationState)
      .where(
        eq(applicationState.key, `channel_delivery_error:${this.channelId}`),
      )
      .get();
    let lastError: { code: string; timestamp: number } | null = null;
    try {
      lastError = z
        .object({ code: z.string().max(80), timestamp: z.number() })
        .parse(JSON.parse(error?.value ?? 'null') as unknown);
    } catch {
      /* No recorded delivery failure. */
    }
    return {
      ...channel,
      canPost:
        !!channel?.canPost &&
        channel.checkedAt !== null &&
        this.now() >= channel.checkedAt &&
        this.now() - channel.checkedAt <= 60_000,
      channelId: this.channelId,
      total: rules.length,
      active: rules.filter((rule) => rule.enabled && this.eligible(rule))
        .length,
      lastSuccess:
        lastSuccess !== null &&
        Number.isSafeInteger(lastSuccess) &&
        lastSuccess >= 0
          ? lastSuccess
          : null,
      lastError,
    };
  }
}
