import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import net from 'node:net';
if(existsSync('.env'))process.loadEnvFile('.env');
const children=[];let stopping=false;
function launch(command,args){const p=spawn(command,args,{stdio:'inherit',env:process.env});children.push(p);p.on('error',e=>{console.error(e.message);stop(1)});p.on('exit',code=>{if(!stopping)stop(code||0)});return p}
function stop(code=0){if(stopping)return;stopping=true;for(const p of children)p.kill('SIGTERM');setTimeout(()=>process.exit(code),2000)}
async function portOpen(port){return new Promise(resolve=>{const s=net.createConnection({host:'127.0.0.1',port});s.once('connect',()=>{s.destroy();resolve(true)});s.once('error',()=>resolve(false));s.setTimeout(500,()=>{s.destroy();resolve(false)})})}
process.on('SIGINT',()=>stop());process.on('SIGTERM',()=>stop());
if(await portOpen(Number(process.env.OD_PORT||8765))){console.error('Option Desk API port already in use. Stop the previous instance first.');process.exit(1)}
if(!await portOpen(55432)){
 launch(process.execPath,['scripts/database.mjs']);
 let ready=false;
 for(let i=0;i<60&&!stopping;i++){if(await portOpen(55432)){ready=true;break}await new Promise(r=>setTimeout(r,500))}
 if(!ready){console.error('Project database did not become ready');stop(1)}
}
if(!stopping){launch('.venv/bin/option-desk',['serve']);launch('.venv/bin/option-desk',['worker'])}
