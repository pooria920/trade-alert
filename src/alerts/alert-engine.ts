import { randomUUID } from 'node:crypto';
import { and, eq, inArray, isNull, isNotNull, or } from 'drizzle-orm';
import type { Logger } from 'pino';
import type { openDatabase } from '../database/client.js';
import {
  alertEvents,
  alertRules,
  alertStates,
  notificationOutbox,
} from '../database/schema.js';
import type { SnapshotRepository } from '../database/repositories/snapshot.repository.js';
import {
  queueSnapshotSchema,
  type QueueSnapshot,
} from '../market/market.types.js';
import type { UserService } from '../users/user-service.js';
import { parseDefinition } from './alert.types.js';
import {
  definitionRecovered,
  evaluateDefinition,
  parseChannelDefinition,
} from './composite.js';
import type { ChannelService } from './channel-service.js';

export class AlertEngine {
  private readonly startedAt: number;
  constructor(
    private readonly db: ReturnType<typeof openDatabase>['db'],
    private readonly snapshots: SnapshotRepository,
    private readonly users: UserService,
    private readonly allowedOwners: string[],
    private readonly maxAgeMs: number,
    private readonly logger: Logger,
    private readonly now = Date.now,
    private readonly channels?: ChannelService,
  ) {
    this.startedAt = now();
  }

  onSnapshot(value: QueueSnapshot, sessionId: string) {
    const parsed = queueSnapshotSchema.safeParse(value);
    const now = this.now();
    if (
      !parsed.success ||
      value.timestamp < this.startedAt ||
      value.timestamp > now ||
      now - value.timestamp > this.maxAgeMs ||
      !this.snapshots.hasAlertSnapshot(value, sessionId)
    )
      return;
    const rules = this.db
      .select()
      .from(alertRules)
      .where(
        and(
          eq(alertRules.insCode, value.insCode),
          eq(alertRules.enabled, true),
          or(
            and(
              isNull(alertRules.channelId),
              inArray(alertRules.ownerId, this.allowedOwners),
            ),
            isNotNull(alertRules.channelId),
          ),
        ),
      )
      .all();
    // All rules share these lookups, including users watching the same instrument.
    const history = new Map<number, QueueSnapshot | null>();
    const at = (timestamp: number) => {
      if (!history.has(timestamp))
        history.set(
          timestamp,
          this.snapshots.alertBaseline(value.insCode, timestamp, sessionId),
        );
      return history.get(timestamp) ?? null;
    };
    for (const rule of rules) {
      try {
        const definition =
          rule.channelId === null
            ? parseDefinition(rule.definition)
            : parseChannelDefinition(rule.definition);
        if (
          !definition ||
          !rule.ownerId ||
          (rule.channelId === null
            ? !this.users.findSymbol(rule.ownerId, value.insCode)
            : !this.channels?.eligible(rule)) ||
          value.timestamp < rule.createdAt
        )
          continue;
        this.db.transaction((tx) => {
          const previousState = tx
            .select()
            .from(alertStates)
            .where(eq(alertStates.ruleId, rule.id))
            .get();
          if (previousState && value.timestamp <= previousState.lastEvaluatedAt)
            return;
          if (
            !previousState ||
            previousState.revision !== rule.revision ||
            previousState.sessionId !== sessionId
          ) {
            tx.insert(alertStates)
              .values({
                ruleId: rule.id,
                revision: rule.revision,
                sessionId,
                status: 'ARMED',
                lastEvaluatedAt: value.timestamp,
                lastTriggeredAt: previousState?.lastTriggeredAt ?? null,
              })
              .onConflictDoUpdate({
                target: alertStates.ruleId,
                set: {
                  revision: rule.revision,
                  sessionId,
                  status: 'ARMED',
                  baselineVolume: null,
                  recoveryData: null,
                  lastEvaluatedAt: value.timestamp,
                },
              })
              .run();
            return; // Prime new/edited/session states; never replay a historical occurrence.
          }
          const state = { ...previousState, lastEvaluatedAt: value.timestamp };
          const cooled =
            state.lastTriggeredAt === null ||
            now - state.lastTriggeredAt >= rule.cooldownSeconds * 1000;
          if (state.status !== 'ARMED') {
            state.status = 'WAITING_FOR_RECOVERY';
            const recovered = definitionRecovered(
              definition,
              value,
              at,
              state.baselineVolume,
              state.recoveryData,
            );
            if (recovered && cooled) {
              state.status = 'ARMED';
              state.baselineVolume = null;
              state.recoveryData = null;
            }
          } else {
            const result = evaluateDefinition(definition, value, at);
            if (result.status === 'TRIGGER' && cooled) {
              state.status = 'TRIGGERED';
              state.lastTriggeredAt = now;
              state.baselineVolume = result.previousVolume;
              state.recoveryData = result.matches
                ? JSON.stringify(result.matches)
                : null;
              const occurrenceKey = `${rule.id}:${rule.revision}:${sessionId}:${value.timestamp}`;
              const payload = {
                symbol: value.symbol,
                type: result.matches ? 'COMPOSITE' : definition.type,
                operator:
                  definition.type === 'GROUP' ? definition.operator : null,
                matches: result.matches,
                timestamp: value.timestamp,
                previousVolume: result.previousVolume,
                currentVolume: result.currentVolume,
                actualPercent: result.actualPercent,
                windowMinutes: result.windowMinutes,
              };
              const id = randomUUID();
              const inserted = tx
                .insert(alertEvents)
                .values({
                  id,
                  ruleId: rule.id,
                  occurrenceKey,
                  ruleRevision: rule.revision,
                  payload: JSON.stringify(payload),
                  message: '',
                  createdAt: now,
                })
                .onConflictDoNothing()
                .run().changes;
              if (inserted)
                tx.insert(notificationOutbox)
                  .values({ eventId: id, nextAttemptAt: now })
                  .run();
            }
          }
          tx.update(alertStates)
            .set(state)
            .where(eq(alertStates.ruleId, rule.id))
            .run();
        });
      } catch (err) {
        this.logger.error({ err, ruleId: rule.id }, 'Rule evaluation failed');
      }
    }
  }
}
