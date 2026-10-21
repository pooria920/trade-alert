import { and, asc, desc, eq, gte, isNotNull, lte, sql } from 'drizzle-orm';
import { z } from 'zod';
import type { openDatabase } from '../client.js';
import { queueSnapshots } from '../schema.js';
import {
  insCodeSchema,
  queueSnapshotSchema,
  type QueueSnapshot,
} from '../../market/market.types.js';

type Database = ReturnType<typeof openDatabase>['db'];
export type StoredSnapshot = QueueSnapshot & { collectedAt: number };
const timestamp = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);

export class SnapshotRepository {
  constructor(
    private readonly db: Database,
    private readonly toleranceMs = 20_000,
  ) {}

  save(
    value: QueueSnapshot,
    collectedAt: number,
    sessionId: string | null = null,
  ): boolean {
    const snapshot = queueSnapshotSchema.parse(value);
    timestamp.parse(collectedAt);
    if (collectedAt < snapshot.timestamp)
      throw new Error('Collection precedes source timestamp');
    return this.db.transaction((tx) => {
      const existing = tx
        .select()
        .from(queueSnapshots)
        .where(
          and(
            eq(queueSnapshots.insCode, snapshot.insCode),
            eq(queueSnapshots.timestamp, snapshot.timestamp),
          ),
        )
        .get();
      if (existing) {
        if (existing.collectedAt === null)
          throw new Error('Source timestamp collides with legacy history');
        if (
          existing.queueType !== snapshot.queueType ||
          existing.volume !== snapshot.volume ||
          existing.price !== snapshot.price ||
          existing.orderCount !== snapshot.orderCount
        )
          throw new Error('Conflicting update at the same source timestamp');
        return false;
      }
      tx.insert(queueSnapshots)
        .values({ ...snapshot, collectedAt, sessionId })
        .run();
      return true;
    });
  }

  // Alert comparisons require persisted session provenance, not inferred legacy timestamps.
  alertBaseline(
    insCode: string,
    at: number,
    sessionId: string,
  ): QueueSnapshot | null {
    const row = this.db
      .select()
      .from(queueSnapshots)
      .where(
        and(
          eq(queueSnapshots.insCode, insCode),
          eq(queueSnapshots.sessionId, sessionId),
          lte(queueSnapshots.timestamp, at),
          gte(queueSnapshots.timestamp, Math.max(0, at - this.toleranceMs)),
          isNotNull(queueSnapshots.collectedAt),
          isNotNull(queueSnapshots.symbol),
        ),
      )
      .orderBy(desc(queueSnapshots.timestamp))
      .limit(1)
      .get();
    const parsed = queueSnapshotSchema.safeParse(row);
    return parsed.success ? parsed.data : null;
  }

  hasAlertSnapshot(snapshot: QueueSnapshot, sessionId: string): boolean {
    const row = this.db
      .select()
      .from(queueSnapshots)
      .where(
        and(
          eq(queueSnapshots.insCode, snapshot.insCode),
          eq(queueSnapshots.timestamp, snapshot.timestamp),
          eq(queueSnapshots.sessionId, sessionId),
        ),
      )
      .get();
    return (
      !!row &&
      row.collectedAt !== null &&
      row.symbol === snapshot.symbol &&
      row.queueType === snapshot.queueType &&
      row.volume === snapshot.volume &&
      row.price === snapshot.price &&
      row.orderCount === snapshot.orderCount
    );
  }

  private query(insCode: string) {
    insCodeSchema.parse(insCode);
    return this.db
      .select({
        insCode: queueSnapshots.insCode,
        symbol: sql<string>`${queueSnapshots.symbol}`,
        timestamp: queueSnapshots.timestamp,
        queueType: queueSnapshots.queueType,
        volume: queueSnapshots.volume,
        price: queueSnapshots.price,
        orderCount: queueSnapshots.orderCount,
        collectedAt: sql<number>`${queueSnapshots.collectedAt}`,
      })
      .from(queueSnapshots)
      .$dynamic()
      .where(
        and(
          eq(queueSnapshots.insCode, insCode),
          isNotNull(queueSnapshots.collectedAt),
          isNotNull(queueSnapshots.symbol),
        ),
      );
  }

  getLatestSnapshot(insCode: string): StoredSnapshot | null {
    return (
      this.query(insCode)
        .orderBy(desc(queueSnapshots.timestamp))
        .limit(1)
        .get() ?? null
    );
  }

  getSnapshotAtOrBefore(insCode: string, at: number): StoredSnapshot | null {
    timestamp.parse(at);
    return (
      this.query(insCode)
        .where(
          and(
            eq(queueSnapshots.insCode, insCode),
            isNotNull(queueSnapshots.collectedAt),
            isNotNull(queueSnapshots.symbol),
            lte(queueSnapshots.timestamp, at),
            gte(queueSnapshots.timestamp, Math.max(0, at - this.toleranceMs)),
          ),
        )
        .orderBy(desc(queueSnapshots.timestamp))
        .limit(1)
        .get() ?? null
    );
  }

  getSnapshotsBetween(
    insCode: string,
    from: number,
    to: number,
  ): StoredSnapshot[] {
    timestamp.parse(from);
    timestamp.parse(to);
    if (from > to) throw new Error('Invalid history range');
    return this.query(insCode)
      .where(
        and(
          eq(queueSnapshots.insCode, insCode),
          isNotNull(queueSnapshots.collectedAt),
          isNotNull(queueSnapshots.symbol),
          gte(queueSnapshots.timestamp, from),
          lte(queueSnapshots.timestamp, to),
        ),
      )
      .orderBy(asc(queueSnapshots.timestamp))
      .all();
  }

  deleteExpired(cutoff: number, batchSize = 1000): number {
    timestamp.parse(cutoff);
    z.number().int().min(1).max(1000).parse(batchSize);
    return this.db.run(
      sql`DELETE FROM queue_snapshots WHERE id IN (SELECT id FROM queue_snapshots WHERE timestamp < ${cutoff} ORDER BY timestamp LIMIT ${batchSize})`,
    ).changes;
  }
}
