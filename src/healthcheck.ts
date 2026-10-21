import { healthReport } from './shared/health.js';

const report = healthReport();
console.log(JSON.stringify(report));
process.exitCode = report.healthy ? 0 : 1;
