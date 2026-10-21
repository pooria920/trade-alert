import { z } from 'zod';
import type { QueueSnapshot } from '../market/market.types.js';

const volume = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const percentage = z
  .number()
  .positive()
  .max(100)
  .refine((n) => Number(n.toFixed(6)) === n, 'At most six decimal places');
const drop = z
  .object({
    thresholdPercent: percentage,
    windowMinutes: z.union([z.literal(1), z.literal(5)]),
    minimumBaselineVolume: volume,
  })
  .strict();
const disappeared = z.object({ minimumPreviousVolume: volume }).strict();
export const ruleDefinitionSchema = z.discriminatedUnion('type', [
  z
    .object({ type: z.literal('BUY_QUEUE_DROP_PERCENT'), config: drop })
    .strict(),
  z
    .object({ type: z.literal('SELL_QUEUE_DROP_PERCENT'), config: drop })
    .strict(),
  z
    .object({ type: z.literal('BUY_QUEUE_DISAPPEARED'), config: disappeared })
    .strict(),
  z
    .object({ type: z.literal('SELL_QUEUE_DISAPPEARED'), config: disappeared })
    .strict(),
]);
export type RuleDefinition = z.infer<typeof ruleDefinitionSchema>;
export type AlertType = RuleDefinition['type'];
export type DropConfig = z.infer<typeof drop>;
export type DisappearedConfig = z.infer<typeof disappeared>;
export interface EvaluationContext {
  current: QueueSnapshot;
  baseline: QueueSnapshot | null;
  previous: QueueSnapshot | null;
}
export type EvaluationResult =
  | { status: 'INSUFFICIENT_DATA' | 'CLEAR' }
  | {
      status: 'TRIGGER';
      previousVolume: number;
      currentVolume: number;
      actualPercent: number;
      windowMinutes: number | null;
    };
export const eventPayloadSchema = z.object({
  symbol: z.string().max(80),
  type: z.enum([
    'BUY_QUEUE_DROP_PERCENT',
    'SELL_QUEUE_DROP_PERCENT',
    'BUY_QUEUE_DISAPPEARED',
    'SELL_QUEUE_DISAPPEARED',
  ]),
  timestamp: volume,
  previousVolume: volume,
  currentVolume: volume,
  actualPercent: z.number().min(0).max(100),
  windowMinutes: z.number().nullable(),
});
export type EventPayload = z.infer<typeof eventPayloadSchema>;
export function parseDefinition(value: string | null): RuleDefinition | null {
  try {
    return ruleDefinitionSchema.parse(JSON.parse(value ?? 'null') as unknown);
  } catch {
    return null;
  }
}
