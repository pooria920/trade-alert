import { defineConfig } from 'drizzle-kit';
import { databasePathSchema } from './src/config/env.js';

export default defineConfig({
  dialect: 'sqlite',
  schema: './src/database/schema.ts',
  out: './src/database/migrations',
  dbCredentials: {
    url: databasePathSchema.parse(process.env.DATABASE_URL),
  },
});
