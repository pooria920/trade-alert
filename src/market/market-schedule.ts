import { z } from 'zod';

const time = z.string().regex(/^(?:[01]\d|2[0-3]):[0-5]\d$/);
export const calendarDateSchema = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/)
  .refine((value) => {
    const date = new Date(`${value}T00:00:00Z`);
    return (
      Number.isFinite(date.getTime()) &&
      date.toISOString().slice(0, 10) === value
    );
  });
export const sessionsSchema = z.array(
  z
    .object({
      market: z.string().trim().min(1).max(50).default('equities'),
      days: z.array(z.number().int().min(1).max(7)).min(1),
      start: time,
      end: time,
    })
    .refine(
      (session) => session.start < session.end,
      'Use separate sessions for overnight hours',
    ),
);
export type MarketSession = z.infer<typeof sessionsSchema>[number];

export class MarketSchedule {
  constructor(
    private readonly sessions: readonly MarketSession[],
    private readonly closedDates: readonly string[] = [],
    readonly forceActive = false,
    private readonly market = 'equities',
  ) {}

  get configured() {
    return this.sessions.some((session) => session.market === this.market);
  }

  isOpen(timestamp: number): boolean {
    return this.sessionId(timestamp) !== null;
  }

  sessionId(timestamp: number): string | null {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Tehran',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(timestamp);
    const part = (type: string) =>
      parts.find((value) => value.type === type)?.value ?? '';
    const date = `${part('year')}-${part('month')}-${part('day')}`;
    if (this.forceActive) return `${this.market}:${date}:forced`;
    if (this.closedDates.includes(date)) return null;
    const weekday = new Date(`${date}T00:00:00Z`).getUTCDay() || 7;
    const clock = `${part('hour')}:${part('minute')}`;
    const session = this.sessions.find(
      (session) =>
        session.market === this.market &&
        session.days.includes(weekday) &&
        clock >= session.start &&
        clock < session.end,
    );
    return session
      ? `${this.market}:${date}:${session.start}:${session.end}`
      : null;
  }
}
