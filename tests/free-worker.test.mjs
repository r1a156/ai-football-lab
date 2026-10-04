import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from '../cloudflare/free-worker.mjs';
test('free health needs no storage or container',async()=>{
 const result=await worker.fetch(new Request('https://x/health'),{},{});
 assert.equal((await result.json()).containers,false);
});
test('public visitors cannot consume AI',async()=>{
 const result=await worker.fetch(new Request('https://x/internal/ai',{method:'POST'}),{AI_ACCESS_TOKEN:'private'},{});
 assert.equal(result.status,401);
});
test('AI calls Cloudflare and returns compatible schema',async()=>{
 const result=await worker.fetch(new Request('https://x/internal/ai',{method:'POST',headers:{Authorization:'Bearer private'},body:JSON.stringify({messages:[{role:'user',content:'test'}]})}),{AI_ACCESS_TOKEN:'private',AI_MODEL:'model',AI:{run:async()=>({response:{ok:true}})}},{});
 assert.equal(JSON.parse((await result.json()).choices[0].message.content).ok,true);
});
test('quota failure is reported for deterministic fallback',async()=>{
 const result=await worker.fetch(new Request('https://x/internal/ai',{method:'POST',headers:{Authorization:'Bearer private'},body:JSON.stringify({messages:[{role:'user',content:'test'}]})}),{AI_ACCESS_TOKEN:'private',AI_MODEL:'model',AI:{run:async()=>{throw Error('Quota')}}},{});
 assert.equal(result.status,503);
});
