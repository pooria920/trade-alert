import { expect, it } from 'vitest';
import { createLogger } from '../src/shared/logger.js';

it('redacts bot tokens from error messages and stacks', () => {
  const token = `123456:${'x'.repeat(35)}`;
  let output = '';
  const destination = {
    write: (chunk: string) => {
      output += chunk;
    },
  };
  createLogger('info', token, destination).error(
    {
      err: new Error(
        `Request failed: https://api.telegram.org/bot${token}/getMe`,
      ),
    },
    'Telegram failure',
  );
  expect(output).not.toContain(token);
  expect(output).toContain('[REDACTED]');
  // The bootstrap logger must also redact without having parsed configuration.
  createLogger('info', '', destination).error(
    { err: new Error(`https://api.telegram.org/bot${token}/getMe`) },
    'Startup failure',
  );
  expect(output).not.toContain(token);
});
