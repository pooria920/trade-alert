import { describe, expect, it } from 'vitest';
import { openDatabase } from '../src/database/client.js';
import {
  alertEvents,
  alertRules,
  symbols,
  users,
} from '../src/database/schema.js';
import { UserService } from '../src/users/user-service.js';
import { instrument } from './fixtures/normalized.js';

describe('user ownership', () => {
  it('isolates watchlists, rule access, removals and notification preferences', () => {
    const { db, sqlite } = openDatabase(':memory:');
    const service = new UserService(db, ['123', '456', '789']);
    try {
      expect(service.preferences('789')).toBeUndefined();
      expect(() => service.addSymbol('789', instrument)).toThrow();
      service.register('123', '123');
      service.register('456', '456');
      service.addSymbol('123', instrument);
      service.addSymbol('456', instrument);
      service.addSymbol('123', instrument);
      expect(service.listSymbols('123')).toHaveLength(1);
      expect(db.select().from(symbols).all()).toHaveLength(1);
      for (const ownerId of ['123', '456'])
        db.insert(alertRules)
          .values({
            id: `rule-${ownerId}`,
            ownerId,
            insCode: instrument.insCode,
            type: 'BUY_QUEUE_DISAPPEARED',
            createdAt: Date.now(),
          })
          .run();
      db.insert(alertRules)
        .values({
          id: 'legacy',
          insCode: instrument.insCode,
          type: 'SELL_QUEUE_DISAPPEARED',
          createdAt: Date.now(),
        })
        .run();
      db.insert(alertEvents)
        .values({
          id: 'event-456',
          ruleId: 'rule-456',
          message: 'هشدار',
          createdAt: Date.now(),
        })
        .run();
      expect(service.listRules('123').map((rule) => rule.id)).toEqual([
        'rule-123',
      ]);
      expect(service.removeRule('123', 'rule-456')).toBe(false);
      expect(service.notificationTarget('123', 'rule-456')).toBeNull();
      expect(service.notificationTarget('456', 'rule-456')).toBe('456');
      expect(service.notificationTarget('123', 'legacy')).toBeNull();
      service.setNotifications('456', false);
      service.register('456', '456');
      expect(service.notificationTarget('456', 'rule-456')).toBeNull();
      expect(service.preferences('123')?.notificationsEnabled).toBe(true);
      expect(service.removeSymbol('123', instrument.insCode)).toBe(true);
      expect(service.listRules('123')).toEqual([]);
      expect(service.listSymbols('456')).toHaveLength(1);
      expect(service.listRules('456')).toHaveLength(1);
      expect(db.select().from(alertEvents).all()).toHaveLength(1);
      expect(
        db
          .select()
          .from(alertRules)
          .all()
          .some((rule) => rule.id === 'legacy'),
      ).toBe(true);
      expect(() => service.listSymbols('999')).toThrow('Unauthorized');
      expect(() => service.register('123', '456')).toThrow('private');
      expect(db.select().from(users).all()).toHaveLength(2);
      const revoked = new UserService(db, ['123']);
      expect(() => revoked.notificationTarget('456', 'rule-456')).toThrow(
        'Unauthorized',
      );
    } finally {
      sqlite.close();
    }
  });
});
