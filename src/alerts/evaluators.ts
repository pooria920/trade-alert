import type {
  AlertType,
  DisappearedConfig,
  DropConfig,
  EvaluationContext,
  EvaluationResult,
  RuleDefinition,
} from './alert.types.js';
import type { QueueSnapshot } from '../market/market.types.js';

export function queueDrop(
  side: 'buy' | 'sell',
  config: DropConfig,
  context: EvaluationContext,
): EvaluationResult {
  const { current, baseline } = context;
  if (
    !baseline ||
    baseline.insCode !== current.insCode ||
    baseline.timestamp >= current.timestamp ||
    baseline.queueType !== side ||
    (current.queueType !== side && current.queueType !== 'none') ||
    baseline.volume <= 0 ||
    baseline.volume < config.minimumBaselineVolume
  )
    return { status: 'INSUFFICIENT_DATA' };
  const lost = baseline.volume - current.volume;
  if (
    lost <= 0 ||
    BigInt(lost) * 100_000_000n <
      BigInt(baseline.volume) *
        BigInt(Math.round(config.thresholdPercent * 1_000_000))
  )
    return { status: 'CLEAR' };
  return {
    status: 'TRIGGER',
    previousVolume: baseline.volume,
    currentVolume: current.volume,
    actualPercent: (lost / baseline.volume) * 100,
    windowMinutes: config.windowMinutes,
  };
}
export function queueDisappeared(
  side: 'buy' | 'sell',
  config: DisappearedConfig,
  context: EvaluationContext,
): EvaluationResult {
  const { current, previous } = context;
  if (
    !previous ||
    previous.insCode !== current.insCode ||
    previous.timestamp >= current.timestamp ||
    previous.queueType !== side ||
    previous.volume <= 0 ||
    previous.volume < config.minimumPreviousVolume
  )
    return { status: 'INSUFFICIENT_DATA' };
  if (current.queueType === side) return { status: 'CLEAR' };
  if (current.queueType !== 'none') return { status: 'INSUFFICIENT_DATA' };
  return {
    status: 'TRIGGER',
    previousVolume: previous.volume,
    currentVolume: 0,
    actualPercent: 100,
    windowMinutes: null,
  };
}
interface Strategy<C> {
  evaluate: (config: C, context: EvaluationContext) => EvaluationResult;
  windowMs: (config: C) => number | null;
  recovered: (
    config: C,
    current: QueueSnapshot,
    baseline: number | null,
  ) => boolean;
}
type Registry = {
  [T in AlertType]: Strategy<Extract<RuleDefinition, { type: T }>['config']>;
};
const dropStrategy = (side: 'buy' | 'sell'): Strategy<DropConfig> => ({
  evaluate: (config, context) => queueDrop(side, config, context),
  windowMs: (config) => config.windowMinutes * 60_000,
  // Five percentage points of hysteresis; small thresholds require full recovery.
  recovered: (config, current, baseline) =>
    current.queueType === side &&
    baseline !== null &&
    BigInt(current.volume) * 100_000_000n >=
      BigInt(baseline) *
        BigInt(
          Math.round(
            (100 - Math.max(0, config.thresholdPercent - 5)) * 1_000_000,
          ),
        ),
});
const disappearedStrategy = (
  side: 'buy' | 'sell',
): Strategy<DisappearedConfig> => ({
  evaluate: (config, context) => queueDisappeared(side, config, context),
  windowMs: () => null,
  recovered: (config, current) =>
    current.queueType === side &&
    current.volume >= Math.max(1, config.minimumPreviousVolume),
});
export const evaluatorRegistry: Registry = {
  BUY_QUEUE_DROP_PERCENT: dropStrategy('buy'),
  SELL_QUEUE_DROP_PERCENT: dropStrategy('sell'),
  BUY_QUEUE_DISAPPEARED: disappearedStrategy('buy'),
  SELL_QUEUE_DISAPPEARED: disappearedStrategy('sell'),
};
// The generic keeps each registry entry's configuration correlated with its discriminant.
export function strategyFor<T extends AlertType>(
  definition: Extract<RuleDefinition, { type: T }>,
) {
  const strategy = evaluatorRegistry[definition.type] as Strategy<
    typeof definition.config
  >;
  return {
    evaluate: (context: EvaluationContext) =>
      strategy.evaluate(definition.config, context),
    windowMs: strategy.windowMs(definition.config),
    recovered: (current: QueueSnapshot, baseline: number | null) =>
      strategy.recovered(definition.config, current, baseline),
  };
}
export const evaluate = (
  definition: RuleDefinition,
  context: EvaluationContext,
) => strategyFor(definition).evaluate(context);
