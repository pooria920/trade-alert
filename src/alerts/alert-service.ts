import { randomUUID } from 'node:crypto';
import { and, desc, eq, isNull } from 'drizzle-orm';
import { z } from 'zod';
import type { openDatabase } from '../database/client.js';
import {
  alertEvents,
  alertRules,
  alertStates,
  notificationOutbox,
} from '../database/schema.js';
import type { UserService } from '../users/user-service.js';
import {
  parseDefinition,
  ruleDefinitionSchema,
  type RuleDefinition,
} from './alert.types.js';

export class AlertService {
  constructor(
    private readonly db: ReturnType<typeof openDatabase>['db'],
    private readonly users: UserService,
    private readonly defaultCooldown = 300,
    private readonly historyLimit = 10,
    private readonly now = Date.now,
  ) {}
  list(owner: string) {
    return this.users.listRules(owner).flatMap((row) => {
      const definition = parseDefinition(row.definition);
      return definition ? [{ ...row, definition }] : [];
    });
  }
  get(owner: string, id: string) {
    return this.list(owner).find((rule) => rule.id === id) ?? null;
  }
  create(
    owner: string,
    insCode: string,
    input: RuleDefinition,
    cooldown = this.defaultCooldown,
  ) {
    const definition = ruleDefinitionSchema.parse(input);
    z.number().int().nonnegative().max(86_400).parse(cooldown);
    if (!this.users.findSymbol(owner, insCode))
      throw new Error('Symbol is not in your watchlist');
    const id = randomUUID();
    this.db
      .insert(alertRules)
      .values({
        id,
        ownerId: owner,
        insCode,
        type: 'BUY_QUEUE_DISAPPEARED',
        definition: JSON.stringify(definition),
        cooldownSeconds: cooldown,
        createdAt: this.now(),
      })
      .run();
    return id;
  }
  update(
    owner: string,
    id: string,
    input: RuleDefinition,
    enabled?: boolean,
    cooldown?: number,
  ) {
    const definition = ruleDefinitionSchema.parse(input);
    const rule = this.get(owner, id);
    if (!rule || !this.users.findSymbol(owner, rule.insCode)) return false;
    if (cooldown !== undefined)
      z.number().int().nonnegative().max(86_400).parse(cooldown);
    this.db.transaction((tx) => {
      tx.update(alertRules)
        .set({
          definition: JSON.stringify(definition),
          revision: rule.revision + 1,
          enabled: enabled ?? rule.enabled,
          cooldownSeconds: cooldown ?? rule.cooldownSeconds,
          createdAt: this.now(),
        })
        .where(and(eq(alertRules.id, id), eq(alertRules.ownerId, owner)))
        .run();
      tx.delete(alertStates).where(eq(alertStates.ruleId, id)).run();
      const events = tx
        .select({ id: alertEvents.id })
        .from(alertEvents)
        .where(eq(alertEvents.ruleId, id))
        .all();
      for (const event of events)
        tx.update(notificationOutbox)
          .set({ status: 'CANCELLED' })
          .where(
            and(
              eq(notificationOutbox.eventId, event.id),
              eq(notificationOutbox.status, 'PENDING'),
            ),
          )
          .run();
    });
    return true;
  }
  setEnabled(owner: string, id: string, enabled: boolean) {
    const rule = this.get(owner, id);
    return rule
      ? rule.enabled === enabled ||
          this.update(owner, id, rule.definition, enabled)
      : false;
  }
  remove(owner: string, id: string) {
    return this.users.removeRule(owner, id);
  }
  history(owner: string) {
    this.users.preferences(owner); // Authorize before querying, including users with no rules.
    return this.db
      .select({ event: alertEvents })
      .from(alertEvents)
      .innerJoin(alertRules, eq(alertRules.id, alertEvents.ruleId))
      .where(and(eq(alertRules.ownerId, owner), isNull(alertRules.channelId)))
      .orderBy(desc(alertEvents.createdAt), desc(alertEvents.id))
      .limit(this.historyLimit)
      .all()
      .map((row) => row.event);
  }
}
