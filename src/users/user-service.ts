import { and, asc, eq, inArray, isNull } from 'drizzle-orm';
import type { openDatabase } from '../database/client.js';
import { alertRules, symbols, users, watchlists } from '../database/schema.js';
import { instrumentSchema, type Instrument } from '../market/market.types.js';

type Database = ReturnType<typeof openDatabase>['db'];

export class UserService {
  private readonly allowed: Set<string>;

  constructor(
    private readonly db: Database,
    allowedUserIds: readonly string[],
  ) {
    this.allowed = new Set(allowedUserIds);
  }

  private authorize(userId: string) {
    if (!this.allowed.has(userId)) throw new Error('Unauthorized user');
  }

  // Global collector input: public instrument metadata, only from authorized memberships.
  monitoredSymbols(): Instrument[] {
    return this.db
      .selectDistinct({ insCode: symbols.insCode, symbol: symbols.symbol })
      .from(watchlists)
      .innerJoin(symbols, eq(symbols.insCode, watchlists.insCode))
      .where(inArray(watchlists.userId, [...this.allowed]))
      .orderBy(asc(symbols.insCode))
      .all()
      .map((instrument) => ({ ...instrument, name: instrument.symbol }));
  }

  register(userId: string, privateChatId: string) {
    this.authorize(userId);
    if (privateChatId !== userId)
      throw new Error('A private conversation is required');
    const now = Date.now();
    this.db
      .insert(users)
      .values({
        telegramUserId: userId,
        privateChatId,
        createdAt: now,
        updatedAt: now,
      })
      .onConflictDoUpdate({
        target: users.telegramUserId,
        set: { privateChatId, updatedAt: now },
      })
      .run();
  }

  listSymbols(userId: string) {
    this.authorize(userId);
    return this.db
      .select({ insCode: symbols.insCode, symbol: symbols.symbol })
      .from(watchlists)
      .innerJoin(symbols, eq(symbols.insCode, watchlists.insCode))
      .where(eq(watchlists.userId, userId))
      .orderBy(asc(symbols.symbol), asc(symbols.insCode))
      .all();
  }

  findSymbol(userId: string, insCode: string) {
    // ponytail: linear personal-watchlist scan; query the composite key if large lists become common.
    return this.listSymbols(userId).find(
      (symbol) => symbol.insCode === insCode,
    );
  }

  addSymbol(userId: string, value: Instrument) {
    this.authorize(userId);
    const instrument = instrumentSchema.parse(value);
    this.db.transaction((tx) => {
      tx.insert(symbols)
        .values({
          insCode: instrument.insCode,
          symbol: instrument.symbol,
          createdAt: Date.now(),
        })
        .onConflictDoNothing()
        .run();
      tx.insert(watchlists)
        .values({ userId, insCode: instrument.insCode, createdAt: Date.now() })
        .onConflictDoNothing()
        .run();
    });
  }

  removeSymbol(userId: string, insCode: string) {
    this.authorize(userId);
    return this.db.transaction((tx) => {
      const removed = tx
        .delete(watchlists)
        .where(
          and(eq(watchlists.userId, userId), eq(watchlists.insCode, insCode)),
        )
        .run().changes;
      if (removed)
        tx.delete(alertRules)
          .where(
            and(
              eq(alertRules.ownerId, userId),
              eq(alertRules.insCode, insCode),
              isNull(alertRules.channelId),
            ),
          )
          .run();
      // Global instrument metadata and market history belong to all users.
      return removed > 0;
    });
  }

  listRules(userId: string) {
    this.authorize(userId);
    return this.db
      .select()
      .from(alertRules)
      .where(and(eq(alertRules.ownerId, userId), isNull(alertRules.channelId)))
      .all();
  }

  removeRule(userId: string, ruleId: string) {
    this.authorize(userId);
    return (
      this.db
        .delete(alertRules)
        .where(
          and(
            eq(alertRules.id, ruleId),
            eq(alertRules.ownerId, userId),
            isNull(alertRules.channelId),
          ),
        )
        .run().changes > 0
    );
  }

  setNotifications(userId: string, enabled: boolean) {
    this.authorize(userId);
    this.db
      .update(users)
      .set({ notificationsEnabled: enabled, updatedAt: Date.now() })
      .where(eq(users.telegramUserId, userId))
      .run();
  }

  preferences(userId: string) {
    this.authorize(userId);
    return this.db
      .select()
      .from(users)
      .where(eq(users.telegramUserId, userId))
      .get();
  }

  notificationTarget(userId: string, ruleId: string): string | null {
    this.authorize(userId);
    const owner = this.preferences(userId);
    const rule = this.db
      .select({ id: alertRules.id })
      .from(alertRules)
      .where(
        and(
          eq(alertRules.id, ruleId),
          eq(alertRules.ownerId, userId),
          isNull(alertRules.channelId),
        ),
      )
      .get();
    return owner?.notificationsEnabled && rule ? owner.privateChatId : null;
  }
}
