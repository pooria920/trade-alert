import { cpSync } from 'node:fs';

cpSync('src/database/migrations', 'dist/database/migrations', {
  recursive: true,
  filter: (path) => !/\d{4}_snapshot\.json$/.test(path),
});
