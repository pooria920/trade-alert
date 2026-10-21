import { z } from 'zod';
import {
  ruleDefinitionSchema,
  type RuleDefinition,
  type EvaluationResult,
  type EvaluationContext,
} from './alert.types.js';
import { strategyFor } from './evaluators.js';
import type { QueueSnapshot } from '../market/market.types.js';

const minimumVolume = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
export const conditionSchema = z.union([
  ruleDefinitionSchema,
  z
    .object({
      type: z.literal('BUY_QUEUE_VOLUME_ABOVE'),
      config: z.object({ minimumVolume }).strict(),
    })
    .strict(),
  z
    .object({
      type: z.literal('SELL_QUEUE_VOLUME_ABOVE'),
      config: z.object({ minimumVolume }).strict(),
    })
    .strict(),
]);
export type RuleCondition = z.infer<typeof conditionSchema>;
export type RuleGroup = {
  type: 'GROUP';
  operator: 'AND' | 'OR';
  conditions: RuleTree[];
};
export type RuleTree = RuleCondition | RuleGroup;
// Bound depth while parsing, before recursion can consume an untrusted tree.
const treeSchema = (depth: number): z.ZodType<RuleTree> =>
  depth === 0
    ? conditionSchema
    : z.union([
        conditionSchema,
        z
          .object({
            type: z.literal('GROUP'),
            operator: z.enum(['AND', 'OR']),
            conditions: z
              .array(z.lazy(() => treeSchema(depth - 1)))
              .min(1)
              .max(8),
          })
          .strict(),
      ]);
export const channelDefinitionSchema = treeSchema(4).superRefine(
  (tree, ctx) => {
    let leaves = 0;
    const visit = (node: RuleTree) => {
      if (node.type === 'GROUP') node.conditions.forEach(visit);
      else leaves++;
    };
    visit(tree);
    if (leaves > 8)
      ctx.addIssue({ code: 'custom', message: 'At most eight conditions' });
  },
);
export function parseChannelDefinition(value: string | null): RuleTree | null {
  try {
    return channelDefinitionSchema.parse(
      JSON.parse(value ?? 'null') as unknown,
    );
  } catch {
    return null;
  }
}
export type Truth = 'TRUE' | 'FALSE' | 'UNKNOWN';
export function combine(operator: 'AND' | 'OR', values: Truth[]): Truth {
  if (operator === 'AND')
    return values.includes('FALSE')
      ? 'FALSE'
      : values.includes('UNKNOWN')
        ? 'UNKNOWN'
        : 'TRUE';
  return values.includes('TRUE')
    ? 'TRUE'
    : values.includes('UNKNOWN')
      ? 'UNKNOWN'
      : 'FALSE';
}
const evidenceSchema = z.object({
  path: z.string(),
  condition: conditionSchema,
  previousVolume: z.number().int().nonnegative(),
  currentVolume: z.number().int().nonnegative(),
  actualPercent: z.number().min(0).max(100),
  windowMinutes: z.number().nullable(),
});
export const evidenceListSchema = z.array(evidenceSchema).max(8);
export type ConditionEvidence = z.infer<typeof evidenceSchema>;
export const compositeEventPayloadSchema = z.object({
  type: z.literal('COMPOSITE'),
  symbol: z.string().max(80),
  timestamp: z.number().int().nonnegative(),
  operator: z.enum(['AND', 'OR']).nullable(),
  matches: evidenceListSchema.min(1),
});
export type CompositeEventPayload = z.infer<typeof compositeEventPayloadSchema>;
type Trigger = Extract<EvaluationResult, { status: 'TRIGGER' }>;
export type TreeResult = EvaluationResult & { matches?: ConditionEvidence[] };
type History = (at: number) => QueueSnapshot | null;
function leaf(
  condition: RuleCondition,
  current: QueueSnapshot,
  history: History,
): EvaluationResult {
  if ('minimumVolume' in condition.config) {
    const side = condition.type === 'BUY_QUEUE_VOLUME_ABOVE' ? 'buy' : 'sell';
    if (current.queueType !== side && current.queueType !== 'none')
      return { status: 'INSUFFICIENT_DATA' };
    return current.volume >= condition.config.minimumVolume
      ? {
          status: 'TRIGGER',
          previousVolume: current.volume,
          currentVolume: current.volume,
          actualPercent: 0,
          windowMinutes: null,
        }
      : { status: 'CLEAR' };
  }
  const strategy = strategyFor(condition as RuleDefinition);
  const context: EvaluationContext = {
    current,
    baseline:
      strategy.windowMs === null
        ? null
        : history(current.timestamp - strategy.windowMs),
    previous: history(current.timestamp - 1),
  };
  return strategy.evaluate(context);
}
function recovered(
  condition: RuleCondition,
  current: QueueSnapshot,
  baseline: number,
): Truth {
  if ('minimumVolume' in condition.config) {
    const side = condition.type === 'BUY_QUEUE_VOLUME_ABOVE' ? 'buy' : 'sell';
    if (current.queueType !== side && current.queueType !== 'none')
      return 'UNKNOWN';
    return BigInt(current.volume) * 100n <
      BigInt(condition.config.minimumVolume) * 95n
      ? 'FALSE'
      : 'TRUE';
  }
  const strategy = strategyFor(condition as RuleDefinition);
  if (strategy.recovered(current, baseline)) return 'FALSE';
  const side = condition.type.startsWith('BUY_') ? 'buy' : 'sell';
  return current.queueType === side || current.queueType === 'none'
    ? 'TRUE'
    : 'UNKNOWN';
}
export function evaluateTree(
  tree: RuleTree,
  current: QueueSnapshot,
  history: History,
  latched: ConditionEvidence[] = [],
): { truth: Truth; matches: ConditionEvidence[] } {
  const matches: ConditionEvidence[] = [];
  const visit = (node: RuleTree, path: string): Truth => {
    if (node.type === 'GROUP')
      return combine(
        node.operator,
        node.conditions.map((child, index) => visit(child, `${path}.${index}`)),
      );
    const saved = latched.find((item) => item.path === path);
    if (saved) return recovered(node, current, saved.previousVolume);
    const result = leaf(node, current, history);
    if (result.status === 'TRIGGER') {
      matches.push({ path, condition: node, ...result });
      return 'TRUE';
    }
    return result.status === 'CLEAR' ? 'FALSE' : 'UNKNOWN';
  };
  const truth = visit(tree, '0');
  return { truth, matches };
}
export function evaluateDefinition(
  definition: RuleTree,
  current: QueueSnapshot,
  history: History,
): TreeResult {
  if (definition.type !== 'GROUP' && !('minimumVolume' in definition.config))
    return leaf(definition, current, history);
  const result = evaluateTree(definition, current, history);
  if (result.truth !== 'TRUE')
    return { status: result.truth === 'FALSE' ? 'CLEAR' : 'INSUFFICIENT_DATA' };
  const first = result.matches[0];
  if (!first) return { status: 'INSUFFICIENT_DATA' };
  const metrics: Trigger = {
    status: 'TRIGGER',
    previousVolume: first.previousVolume,
    currentVolume: first.currentVolume,
    actualPercent: first.actualPercent,
    windowMinutes: first.windowMinutes,
  };
  return { ...metrics, matches: result.matches };
}
export function definitionRecovered(
  definition: RuleTree,
  current: QueueSnapshot,
  history: History,
  baseline: number | null,
  data: string | null,
): boolean {
  if (definition.type !== 'GROUP' && !('minimumVolume' in definition.config))
    return strategyFor(definition as RuleDefinition).recovered(
      current,
      baseline,
    );
  try {
    const matches = evidenceListSchema.parse(
      JSON.parse(data ?? 'null') as unknown,
    );
    return (
      matches.length > 0 &&
      evaluateTree(definition, current, history, matches).truth === 'FALSE'
    );
  } catch {
    return false;
  }
}
