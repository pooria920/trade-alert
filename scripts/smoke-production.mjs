import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  readdirSync,
  unlinkSync,
  rmdirSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import process from 'node:process';
import console from 'node:console';

// Local Docker only. No host .env, network, production volumes or real credentials are used.
const image = 'tse-stock-alert-bot:local';
const volume = `tse-smoke-${randomUUID()}`;
const container = `tse-smoke-${randomUUID()}`;
const directory = mkdtempSync(join(tmpdir(), 'tse-smoke-'));
const backups = join(directory, 'backups');
mkdirSync(backups, { mode: 0o777 });
const mock = join(directory, 'mock.mjs');
const dockerEnvironment = { ...process.env };
const docker = (args, options = {}) =>
  execFileSync('docker', args, {
    encoding: 'utf8',
    env: dockerEnvironment,
    ...options,
  }).trim();
const hardened = [
  '--network',
  'none',
  '--read-only',
  '--cap-drop',
  'ALL',
  '--security-opt',
  'no-new-privileges',
  '--tmpfs',
  '/tmp:rw,noexec,nosuid,size=32m,mode=1777',
  '--memory',
  '512m',
  '--cpus',
  '1',
  '--pids-limit',
  '128',
  '--mount',
  `type=volume,source=${volume},target=/app/data`,
];
const environment = [
  '-e',
  'TELEGRAM_BOT_TOKEN=123456:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  '-e',
  'TELEGRAM_ALLOWED_USER_IDS=123,456',
  '-e',
  'TELEGRAM_ADMIN_USER_IDS=123',
  '-e',
  'TELEGRAM_CHANNEL_ID=-1001234567890',
  '-e',
  'MARKET_SESSIONS_JSON=[]',
  '-e',
  'MARKET_MONITOR_FORCE_ACTIVE=false',
];
const execute = (args) => docker(['exec', container, ...args]);
const node = (program) =>
  execute(['node', '--input-type=module', '-e', program]);
const once = (args, extra = []) =>
  docker(['run', '--rm', ...hardened, ...extra, image, ...args]);
const rejected = (action) => {
  assert.throws(action);
};
writeFileSync(
  mock,
  `
import {readFileSync,writeFileSync,existsSync} from 'node:fs';
globalThis.fetch=async(url,options={})=>{
 if(!String(url).startsWith('https://api.telegram.org/bot'))throw new Error('Unexpected offline network call');
 const method=String(url).split('/').at(-1); let result=true;
 if(method==='getMe')result={id:123456,is_bot:true,first_name:'Offline test',username:'offline_test_bot'};
 else if(method==='getChat')result={id:-1001234567890,type:'channel',title:'Synthetic channel'};
 else if(method==='getChatMember')result={status:'administrator',can_post_messages:true};
 else if(method==='getUpdates'){
  await new Promise((done,reject)=>{const stop=()=>{clearTimeout(timer);reject(new DOMException('Aborted','AbortError'));};const timer=setTimeout(()=>{options.signal?.removeEventListener('abort',stop);done();},300);options.signal?.addEventListener('abort',stop,{once:true});if(options.signal?.aborted)stop();});result=[];
 }else if(method==='sendMessage'){
  const payload=JSON.parse(options.body);const file='/app/data/mock-sends.json';const sends=existsSync(file)?JSON.parse(readFileSync(file,'utf8')):[];sends.push(String(payload.chat_id));writeFileSync(file,JSON.stringify(sends));result={message_id:sends.length};
 }else if(!['setMyCommands','deleteWebhook'].includes(method))throw new Error('Unexpected mock method');
 return new Response(JSON.stringify({ok:true,result}),{status:200,headers:{'content-type':'application/json'}});
};
// grammY's Node adapter uses node-fetch v2, not the global Fetch API.
const {createRequire}=await import('node:module');
const appRequire=createRequire('/app/package.json');
const grammyRequire=createRequire(appRequire.resolve('grammy'));
grammyRequire('node-fetch').default=globalThis.fetch;
`,
  { mode: 0o644 },
);
const seed = `
import assert from 'node:assert/strict';import {existsSync,readdirSync} from 'node:fs';
import {openDatabase} from './dist/database/client.js';import {UserService} from './dist/users/user-service.js';import {AlertService} from './dist/alerts/alert-service.js';import {ChannelService} from './dist/alerts/channel-service.js';import {AlertEngine} from './dist/alerts/alert-engine.js';import {SnapshotRepository} from './dist/database/repositories/snapshot.repository.js';import {notificationOutbox} from './dist/database/schema.js';import {createLogger} from './dist/shared/logger.js';
assert.notEqual(process.getuid(),0);for(const path of ['/app/.env','/app/src','/app/tests','/app/node_modules/typescript','/app/node_modules/tsx','/app/node_modules/vitest'])assert.equal(existsSync(path),false,path);assert.equal(readdirSync('/app/dist',{recursive:true}).some(p=>String(p).endsWith('.map')||String(p).endsWith('_snapshot.json')),false);
const {db,sqlite}=openDatabase('/app/data/stock-alert.db');try{
 const users=new UserService(db,['123','456']);for(const id of ['123','456'])users.register(id,id);
 const instrument={insCode:'12345678901234567890',symbol:'synthetic-production-smoke',name:'synthetic-production-smoke'};users.addSymbol('456',instrument);
 let now=Date.now()-60000;const clock=()=>now;const channels=new ChannelService(db,users,['123'],'-1001234567890',async()=>({channelId:'-1001234567890',title:'Synthetic channel',canPost:true}),300,clock);await channels.refresh();
 const definition={type:'BUY_QUEUE_DROP_PERCENT',config:{thresholdPercent:30,windowMinutes:1,minimumBaselineVolume:0}};new AlertService(db,users,300,10,clock).create('456',instrument.insCode,definition);channels.create('123',instrument,{type:'GROUP',operator:'AND',conditions:[definition,{type:'BUY_QUEUE_VOLUME_ABOVE',config:{minimumVolume:500}}]});
 const snapshots=new SnapshotRepository(db);const engine=new AlertEngine(db,snapshots,users,['123','456'],30000,createLogger('silent'),clock,channels);
 for(const volume of [1000,700]){const snapshot={insCode:instrument.insCode,symbol:instrument.symbol,timestamp:now,queueType:'buy',volume,orderCount:1,price:110};snapshots.save(snapshot,now,'synthetic-session');engine.onSnapshot(snapshot,'synthetic-session');now+=60000;}assert.equal(db.select().from(notificationOutbox).all().length,2);
}finally{sqlite.close();}`;
let created = false;
const start = (database) =>
  docker([
    'run',
    '-d',
    '--name',
    container,
    ...hardened,
    ...environment,
    '-e',
    `DATABASE_URL=/app/data/${database}`,
    '--mount',
    `type=bind,source=${mock},target=/mock.mjs,readonly`,
    image,
    'node',
    '--import',
    '/mock.mjs',
    'dist/main.js',
  ]);
const stop = () => {
  docker(['stop', '-t', '20', container]);
  assert.equal(
    docker(['inspect', '--format', '{{.State.ExitCode}}', container]),
    '0',
  );
  docker(['rm', container]);
};
try {
  const endpoint =
    process.env.DOCKER_HOST && !process.env.DOCKER_CONTEXT
      ? process.env.DOCKER_HOST
      : docker([
          'context',
          'inspect',
          '--format',
          '{{.Endpoints.docker.Host}}',
        ]);
  assert.match(
    endpoint,
    /^(unix|npipe):\/\//,
    'Smoke test requires a local Docker socket, not a remote daemon',
  );
  dockerEnvironment.DOCKER_HOST = endpoint;
  delete dockerEnvironment.DOCKER_CONTEXT;
  docker(['volume', 'create', volume]);
  created = true;
  console.log(
    docker(
      ['run', '--rm', '-i', ...hardened, image, 'node', '--input-type=module'],
      { input: seed },
    ),
  );
  start('stock-alert.db');
  node('await new Promise(r=>setTimeout(r,3500));');
  console.log(execute(['node', 'dist/healthcheck.js']));
  node(
    `import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';assert.deepEqual(JSON.parse(readFileSync('/app/data/mock-sends.json','utf8')).sort(),['-1001234567890','456'].sort());`,
  );
  rejected(() => execute(['node', 'dist/database/migrate.js']));
  rejected(() => execute(['node', 'dist/main.js']));
  console.log(execute(['node', 'dist/healthcheck.js']));
  console.log(
    once(
      [
        'node',
        'dist/database/backup.js',
        'backup',
        '/app/data/stock-alert.db',
        '/backups',
      ],
      ['--mount', `type=bind,source=${backups},target=/backups`],
    ),
  );
  const file = readdirSync(backups).find((name) => name.endsWith('.db'));
  assert.ok(file);
  const mount = [
    '--mount',
    `type=bind,source=${backups},target=/backups,readonly`,
  ];
  rejected(() =>
    once(
      [
        'node',
        'dist/database/backup.js',
        'restore',
        `/backups/${file}`,
        '/app/data/stock-alert.db',
        '--confirm-restore',
      ],
      mount,
    ),
  );
  stop();
  console.log(
    once(
      [
        'node',
        'dist/database/backup.js',
        'restore',
        `/backups/${file}`,
        '/app/data/restored.db',
        '--confirm-restore',
      ],
      mount,
    ),
  );
  console.log(
    once(
      ['node', 'dist/database/migrate.js'],
      ['-e', 'DATABASE_URL=/app/data/restored.db'],
    ),
  );
  start('restored.db');
  node('await new Promise(r=>setTimeout(r,1500));');
  console.log(execute(['node', 'dist/healthcheck.js']));
  node(
    `import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';import {openDatabase} from './dist/database/client.js';import {alertRules,alertStates,notificationOutbox} from './dist/database/schema.js';const {db,sqlite}=openDatabase('/app/data/restored.db');try{assert.equal(db.select().from(alertRules).all().length,2);assert.equal(db.select().from(alertStates).all().length,2);assert.ok(db.select().from(notificationOutbox).all().every(row=>row.status==='DELIVERED'));assert.equal(JSON.parse(readFileSync('/app/data/mock-sends.json','utf8')).length,2);assert.deepEqual(sqlite.pragma('foreign_key_check'),[]);}finally{sqlite.close();}`,
  );
  stop();
  console.log(
    'PASS: production-only non-root image; read-only/capability/resource restrictions; mocked real startup and private/channel outbox recovery; closed-market health; duplicate/migration exclusion; online WAL backup; read-only backup restore; migration replay; durable state; no duplicate send; SIGTERM exit 0. All containers had network disabled.',
  );
} catch (error) {
  try {
    console.error(docker(['logs', '--tail', '30', container]));
  } catch {
    /* startup may have failed */
  }
  throw error;
} finally {
  try {
    docker(['rm', '-f', container], { stdio: 'ignore' });
  } catch {
    /* already stopped */
  }
  if (created) docker(['volume', 'rm', volume]);
  // No recursive host deletion: remove only this test's known artifacts and empty directories.
  for (const name of readdirSync(backups))
    if (/^stock-alert-[a-f\d]{12}-[\dTZ.-]+-[a-f\d-]{36}\.db$/.test(name))
      unlinkSync(join(backups, name));
  unlinkSync(mock);
  if (readdirSync(backups).length === 0) rmdirSync(backups);
  if (readdirSync(directory).length === 0) rmdirSync(directory);
}
