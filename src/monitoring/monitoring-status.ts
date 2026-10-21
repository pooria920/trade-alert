export interface SymbolMonitoringStatus {
  insCode: string;
  consecutiveErrors: number;
  lastSuccessfulFetch: number | null;
  lastVerifiedSnapshot: number | null;
  lastResult:
    'none' | 'transport-error' | 'invalid-data' | 'stale-data' | 'fresh-data';
  reason: string | null;
  retryAt: number;
}

export interface MonitoringStatus {
  isRunning: boolean;
  lastProgressAt: number | null;
  isMarketOpen: boolean;
  scheduleConfigured: boolean;
  forceActive: boolean;
  monitoredSymbols: number;
  freshSymbols: number;
  pollIntervalMs: number;
  maxAgeMs: number;
  lastPollingCycle: number | null;
  lastSuccessfulFetch: number | null;
  lastVerifiedSnapshot: number | null;
  consecutiveErrors: number;
  rateLimitedUntil: number;
  storedSnapshots: number;
  symbols: SymbolMonitoringStatus[];
}
