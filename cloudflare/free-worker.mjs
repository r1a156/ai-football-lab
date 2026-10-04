import { aiInput, PUBLIC_FILES } from './policy.mjs';
const json=(value,status=200)=>Response.json(value,{status,headers:{'Cache-Control':'no-store'}});
export default {
 async fetch(request,env,ctx){
  const url=new URL(request.url);
  if(url.pathname==='/internal/ai'){
const aiRequestForFormat=request.clone(); // AI_FOOTBALL_CF_RESPONSE_FORMAT_BRIDGE
   if(request.method!=='POST'||!env.AI_ACCESS_TOKEN||request.headers.get('authorization')!=='Bearer '+env.AI_ACCESS_TOKEN)return json({error:'Unauthorized'},401);
   const body=await request.text();
   if(body.length>180000)return json({error:'Input too large'},413);
   try {
    const input=aiInput(JSON.parse(body));
    const aiExtra=await aiRequestForFormat.json().catch(()=>({}));
if(
  aiExtra &&
  aiExtra.response_format &&
  typeof aiExtra.response_format==='object'
){
  input.response_format=aiExtra.response_format;
}
const result=await env.AI.run(env.AI_MODEL,input);
    if(result.response===undefined)throw Error('Missing model response');
    return json({id:crypto.randomUUID(),model:env.AI_MODEL,choices:[{message:{role:'assistant',content:typeof result.response==='string'?result.response:JSON.stringify(result.response)}}],usage:result.usage||{}});
   } catch {return json({error:'Workers AI unavailable or free quota exhausted; use deterministic fallback'},503);}
  }
  if(url.pathname==='/health')return json({service:'ai-football-free',aiProvider:'Cloudflare Workers AI',compute:'GitHub Actions',containers:false});
  if(request.method==='OPTIONS')return new Response(null,{status:204,headers:{'Access-Control-Allow-Origin':env.PUBLIC_ORIGIN,'Access-Control-Allow-Methods':'GET, HEAD, OPTIONS'}});
  if(!['GET','HEAD'].includes(request.method))return json({error:'Method not allowed'},405);
  const name=url.pathname.slice('/data/'.length);
  if(!url.pathname.startsWith('/data/')||!PUBLIC_FILES.includes(name))return json({error:'Not found'},404);
  const key=new Request(url.origin+'/data/'+name);
  const cache=caches.default;
  let response=await cache.match(key);
  if(!response){
   const upstream=await fetch('https://raw.githubusercontent.com/r1a156/ai-football-lab/main/data/'+name,{cf:{cacheTtl:60,cacheEverything:true}});
   if(!upstream.ok)return json({error:'Source unavailable',status:upstream.status},502);
   response=new Response(upstream.body,{headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'public, max-age=60','X-Football-Source':'GitHub Actions'}});
   ctx.waitUntil(cache.put(key,response.clone()));
  }
  const headers=new Headers(response.headers);
  headers.set('Access-Control-Allow-Origin',env.PUBLIC_ORIGIN);
  headers.set('Vary','Origin');
  headers.set('Cache-Control','no-cache');
  return new Response(request.method==='HEAD'?null:response.body,{headers});
 }
};
