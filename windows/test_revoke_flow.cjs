const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync('windows/desktop.js','utf8');
vm.runInThisContext(src.slice(src.indexOf('async function revokeAndConfirm'),src.indexOf('async function bridge')));
(async()=>{
assert.equal((await revokeAndConfirm('a',async()=>({status:'revoked'}))).status,'revoked');
assert.equal((await revokeAndConfirm('a',async(path)=>{if(path==='/api/revoke')throw TypeError('Failed to fetch');return {devices:[]};})).status,'confirmed_absent');
await assert.rejects(revokeAndConfirm('a',async(path)=>{if(path==='/api/revoke')throw TypeError('Failed to fetch');return {devices:[{id:'a'}]};}),/仍在授权/);
await assert.rejects(revokeAndConfirm('a',async()=>{throw TypeError('Failed to fetch');}),/无法连接接收服务/);
await assert.rejects(revokeAndConfirm('a',async(path)=>{if(path==='/api/revoke')throw Error('授权记录保存失败');return {devices:[{id:'a'}]};}),/授权记录保存失败/);
console.log('5 frontend revoke scenarios passed');
})().catch(e=>{console.error(e);process.exitCode=1});
