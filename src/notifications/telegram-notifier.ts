import type { AlertType, EventPayload } from '../alerts/alert.types.js';
import type {
  CompositeEventPayload,
  RuleTree,
  RuleCondition,
} from '../alerts/composite.js';

export const alertLabels: Record<AlertType, string> = {
  BUY_QUEUE_DROP_PERCENT: 'کاهش صف خرید',
  SELL_QUEUE_DROP_PERCENT: 'کاهش صف فروش',
  BUY_QUEUE_DISAPPEARED: 'ناپدید شدن صف خرید',
  SELL_QUEUE_DISAPPEARED: 'ناپدید شدن صف فروش',
};
export const tehranTime = (timestamp: number) =>
  new Intl.DateTimeFormat('fa-IR', {
    timeZone: 'Asia/Tehran',
    dateStyle: 'short',
    timeStyle: 'medium',
  }).format(timestamp);
export const conditionLabels = {
  ...alertLabels,
  BUY_QUEUE_VOLUME_ABOVE: 'حداقل حجم صف خرید',
  SELL_QUEUE_VOLUME_ABOVE: 'حداقل حجم صف فروش',
};
export function conditionText(condition: RuleCondition) {
  const config = condition.config;
  if ('minimumVolume' in config)
    return `${conditionLabels[condition.type]}: ${config.minimumVolume.toLocaleString('fa-IR')} سهم`;
  if ('thresholdPercent' in config)
    return `${conditionLabels[condition.type]}: حداقل ${config.thresholdPercent.toLocaleString('fa-IR')}٪ در ${config.windowMinutes.toLocaleString('fa-IR')} دقیقه؛ حجم مبنا حداقل ${config.minimumBaselineVolume.toLocaleString('fa-IR')} سهم`;
  return `${conditionLabels[condition.type]}؛ حجم قبلی حداقل ${config.minimumPreviousVolume.toLocaleString('fa-IR')} سهم`;
}
export function treeText(tree: RuleTree): string {
  return tree.type === 'GROUP'
    ? `${tree.operator === 'AND' ? 'همه شرط‌ها برقرار باشند' : 'حداقل یکی از شرط‌ها برقرار باشد'}:\n${tree.conditions.map((child, index) => `${(index + 1).toLocaleString('fa-IR')}. ${treeText(child)}`).join('\n')}`
    : conditionText(tree);
}
export function formatNotification(
  payload: EventPayload | CompositeEventPayload,
  now: number,
  maxAgeMs: number,
) {
  const number = (n: number) =>
    n.toLocaleString('fa-IR', { maximumFractionDigits: 2 });
  if (payload.type === 'COMPOSITE')
    return `هشدار بورس\nنماد: ${payload.symbol}\n${payload.operator === 'AND' ? 'همه شرط‌ها برقرار شدند' : payload.operator === 'OR' ? 'حداقل یکی از شرط‌ها برقرار شد' : 'شرط برقرار شد'}\n\n${payload.matches.map((match) => `${conditionText(match.condition)}\n${'minimumVolume' in match.condition.config ? `حجم فعلی صف: ${number(match.currentVolume)} سهم` : `حجم قبلی: ${number(match.previousVolume)}؛ حجم فعلی: ${number(match.currentVolume)} سهم\nمیزان کاهش: ${number(match.actualPercent)}٪`}`).join('\n\n')}\nزمان داده: ${tehranTime(payload.timestamp)} به وقت تهران${now - payload.timestamp > maxAgeMs ? '\nارسال با تأخیر؛ مربوط به زمان دادهٔ بالا است.' : ''}`;
  return `هشدار ${alertLabels[payload.type]}\n\nنماد: ${payload.symbol}\nحجم قبلی صف: ${number(payload.previousVolume)} سهم\nحجم فعلی صف: ${number(payload.currentVolume)} سهم\nمیزان کاهش: ${number(payload.actualPercent)}٪\nبازه مقایسه: ${payload.windowMinutes === null ? 'آخرین وضعیت تأییدشده' : `${number(payload.windowMinutes)} دقیقه`}\nزمان داده: ${tehranTime(payload.timestamp)} به وقت تهران${now - payload.timestamp > maxAgeMs ? '\nارسال با تأخیر؛ این هشدار مربوط به زمان دادهٔ بالا است.' : ''}`;
}
