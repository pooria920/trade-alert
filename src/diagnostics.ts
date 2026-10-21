import { parseEnv } from './config/env.js';
import { healthReport } from './shared/health.js';

try {
  const env = parseEnv(process.env);
  console.log(
    JSON.stringify(
      {
        configuration: 'VALID',
        node: process.versions.node,
        production: env.NODE_ENV === 'production',
        authorizedUsers: env.TELEGRAM_ALLOWED_USER_IDS.length,
        administrators: env.TELEGRAM_ADMIN_USER_IDS.length,
        channelConfigured: !!env.TELEGRAM_CHANNEL_ID,
        scheduleConfigured: env.MARKET_SESSIONS_JSON.length > 0,
        scheduleBypass: env.MARKET_MONITOR_FORCE_ACTIVE,
        liveBookContractVerified: false,
        health: healthReport(),
      },
      null,
      2,
    ),
  );
} catch {
  console.error(
    'Configuration invalid. Check required secrets, authorized IDs, channel settings and production restrictions; values are not printed.',
  );
  process.exitCode = 1;
}
