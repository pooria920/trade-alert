import type { MonitoringStatus } from '../../src/monitoring/monitoring-status.js';
export const fixedNow = Date.parse('2026-10-21T06:30:00Z');
export function makeStatus(
  overrides: Partial<MonitoringStatus> = {},
): MonitoringStatus {
  return {
    isRunning: true,
    lastProgressAt: fixedNow,
    isMarketOpen: true,
    scheduleConfigured: true,
    forceActive: false,
    monitoredSymbols: 1,
    freshSymbols: overrides.lastVerifiedSnapshot ? 1 : 0,
    pollIntervalMs: 10000,
    maxAgeMs: 30000,
    lastPollingCycle: fixedNow,
    lastSuccessfulFetch: null,
    lastVerifiedSnapshot: null,
    consecutiveErrors: 0,
    rateLimitedUntil: 0,
    storedSnapshots: 0,
    symbols: [],
    ...overrides,
  };
}
