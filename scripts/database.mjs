import EmbeddedPostgres from 'embedded-postgres';
import {existsSync, mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
if(existsSync('.env'))process.loadEnvFile('.env');
const dir=resolve('.state/postgres');
mkdirSync(resolve('.state'),{recursive:true});
const pg=new EmbeddedPostgres({databaseDir:dir,user:'optiondesk',password:'optiondesk',port:55432,persistent:true,postgresFlags:['-c','listen_addresses=127.0.0.1'],onLog:()=>{},onError:console.error});
if(!existsSync(resolve(dir,'PG_VERSION'))) await pg.initialise();
await pg.start();
const client=pg.getPgClient();await client.connect();
const configured=process.env.OD_DATABASE_URL?new URL(process.env.OD_DATABASE_URL.replace('postgresql+psycopg:','postgresql:')).pathname.slice(1):'optiondesk';
for(const name of new Set(['optiondesk',configured])){
 if(!/^[a-z][a-z0-9_]*$/.test(name))throw new Error('Invalid project database name');
 const found=await client.query('SELECT 1 FROM pg_database WHERE datname=$1',[name]);
 if(!found.rowCount)await pg.createDatabase(name);
}
await client.end();
console.log('Option Desk PostgreSQL ready: 127.0.0.1:55432');
let stopping=false;
async function stop(){if(stopping)return;stopping=true;await pg.stop();process.exit(0)}
process.on('SIGINT',stop);process.on('SIGTERM',stop);
setInterval(()=>{},60000);
