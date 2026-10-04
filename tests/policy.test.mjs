import {test} from 'node:test';
import assert from 'node:assert/strict';
import {operationalDay, selectMode, aiInput} from '../cloudflare/policy.mjs';
test('operational day rolls over at 08 Moscow',()=>{
 assert.equal(operationalDay(Date.parse('2026-10-04T04:59:00Z')),'2026-10-03');
 assert.equal(operationalDay(Date.parse('2026-10-04T05:00:00Z')),'2026-10-04');
});
test('generation retry cap preserves live settlement',()=>{
 const now=Date.parse('2026-10-04T09:00:00Z');
 assert.equal(selectMode(now,{}),'generate');
 assert.equal(selectMode(now,{attemptDay:'2026-10-04',attempts:4,historyDay:'2026-10-04'}),'live');
 assert.equal(selectMode(now,{publishedDay:'2026-10-04',historyDay:'2026-10-04'}),'live');
});
test('Workers AI gets schema, not OpenAI wrapper; output tokens capped',()=>{
 const schema={type:'object',properties:{result:{type:'string'}}};
 const out=aiInput({messages:[{role:'user',content:'test'}],max_tokens:99999,response_format:{type:'json_schema',json_schema:{name:'audit',schema}}});
 assert.deepEqual(out.response_format.json_schema,schema);
 assert.equal(out.max_tokens,5000);
 assert.throws(()=>aiInput({messages:[{role:'invalid',content:'test'}]}));
});
