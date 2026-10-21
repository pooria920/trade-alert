import { rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, URL } from 'node:url';
import process from 'node:process';
const root = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(new URL('../dist', import.meta.url));
if (!output.startsWith(root) || output === root)
  throw new Error('Invalid build output directory');
rmSync(output, { recursive: true, force: true });
execFileSync(
  process.execPath,
  ['node_modules/typescript/bin/tsc', '-p', 'tsconfig.build.json'],
  { cwd: root, stdio: 'inherit' },
);
await import('./copy-migrations.mjs');
