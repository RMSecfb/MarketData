const OWNER = 'RMSecfb';
const REPO = 'MarketData';
const BRANCH = 'main';
const API = `https://api.github.com/repos/${OWNER}/${REPO}/contents`;

function headers(env){
  if(!env.GITHUB_TOKEN) throw new Error('Cloudflare 尚未設定 GITHUB_TOKEN Secret');
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: 'application/vnd.github+json',
    'Content-Type': 'application/json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'RMSecfb-MarketData-Cloudflare'
  };
}

function toBase64Utf8(text){
  const bytes = new TextEncoder().encode(text);
  let binary='';
  for(const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function fromBase64Utf8(value){
  const binary = atob((value || '').replace(/\n/g,''));
  const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

async function parseGitHubError(resp){
  try{
    const j=await resp.json();
    return j.message || `GitHub HTTP ${resp.status}`;
  }catch{
    return `GitHub HTTP ${resp.status}`;
  }
}

export async function readJson(env, path, {allow404=false}={}){
  const resp = await fetch(`${API}/${path}?ref=${encodeURIComponent(BRANCH)}`, {
    headers: headers(env),
    cf: {cacheTtl: 0, cacheEverything: false}
  });
  if(resp.status===404 && allow404) return {exists:false, sha:null, data:null};
  if(!resp.ok) throw new Error(await parseGitHubError(resp));
  const meta=await resp.json();
  const text=fromBase64Utf8(meta.content || '');
  return {exists:true, sha:meta.sha, data:JSON.parse(text)};
}

async function putJsonOnce(env, path, data, message, sha=null){
  const body={
    message,
    content:toBase64Utf8(JSON.stringify(data,null,2)+'\n'),
    branch:BRANCH,
    ...(sha?{sha}:{})
  };
  const resp=await fetch(`${API}/${path}`, {
    method:'PUT', headers:headers(env), body:JSON.stringify(body)
  });
  if(!resp.ok){
    const error=await parseGitHubError(resp);
    const e=new Error(error); e.status=resp.status; throw e;
  }
  return await resp.json();
}

export async function updateJson(env, path, mutator, message, {createIfMissing=true, retries=1}={}){
  let lastErr;
  for(let attempt=0; attempt<=retries; attempt++){
    try{
      const current=await readJson(env,path,{allow404:createIfMissing});
      const base=current.exists ? current.data : {};
      const next=await mutator(structuredClone(base), current);
      const result=await putJsonOnce(env,path,next,message,current.sha);
      return {data:next, result};
    }catch(e){
      lastErr=e;
      if(e.status!==409 || attempt>=retries) throw e;
    }
  }
  throw lastErr;
}

export function jsonResponse(body,status=200){
  return new Response(JSON.stringify(body),{
    status,
    headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'}
  });
}

export function requirePost(request){
  if(request.method!=='POST') return jsonResponse({ok:false,error:'Method not allowed'},405);
  return null;
}

export function validDate(s){
  return typeof s==='string' && /^\d{4}-\d{2}-\d{2}$/.test(s) && !Number.isNaN(Date.parse(`${s}T00:00:00Z`));
}

export function actorFromRequest(request){
  return request.headers.get('CF-Access-Authenticated-User-Email') || 'Cloudflare Pages user';
}

export function requireAccess(request, env){
  // 建議用 Cloudflare Access 保護整個 Pages 網站或至少 /api/*。
  // 設 REQUIRE_CF_ACCESS=true 後，未帶 Access 使用者標頭的寫入會被拒絕。
  if(String(env.REQUIRE_CF_ACCESS || '').toLowerCase() !== 'true') return null;
  const email=request.headers.get('CF-Access-Authenticated-User-Email');
  if(!email) return jsonResponse({ok:false,error:'未通過 Cloudflare Access 驗證'},401);
  return null;
}
