import {
  readJson, updateJson, jsonResponse, requirePost,
  validDate, actorFromRequest, requireAccess
} from '../_lib/github.js';

const CDS_META = {
  US:{name:'美國',flag:'🇺🇸'},
  KR:{name:'南韓',flag:'🇰🇷'},
  JP:{name:'日本',flag:'🇯🇵'},
  CN:{name:'中國',flag:'🇨🇳'},
  HK:{name:'香港',flag:'🇭🇰'}
};
const INDICATORS = new Set(['vix','move','y2','y10','y30','sox']);
const round2 = n => Math.round(n * 100) / 100;

function cleanManualRecord(rec, reportDate, actor){
  if(!rec || typeof rec !== 'object') return null;
  const value = Number(rec.value);
  if(!Number.isFinite(value)) return null;
  const dataDate = validDate(rec.data_date) ? rec.data_date : reportDate;
  return {
    value,
    data_date: dataDate,
    updated_at: new Date().toISOString(),
    updated_by: actor
  };
}

function validateConfig(cfg){
  if(!cfg || typeof cfg !== 'object') throw new Error('config 格式錯誤');
  if(!cfg.thresholds || typeof cfg.thresholds !== 'object') throw new Error('缺少 thresholds');
  if(!Array.isArray(cfg.stocks)) throw new Error('stocks 必須為陣列');
  if(cfg.recipients != null && !Array.isArray(cfg.recipients)) throw new Error('recipients 必須為陣列');
  return cfg;
}

async function writeCDS(body, env, actor){
  const reportDate = body.report_date;
  if(!validDate(reportDate)) return jsonResponse({ok:false,error:'report_date 格式錯誤'},400);
  if(!body.cds || typeof body.cds !== 'object') return jsonResponse({ok:false,error:'缺少 cds 資料'},400);

  const values = {};
  for(const [key,value] of Object.entries(body.cds)){
    if(!CDS_META[key]) continue;
    const n = Number(value);
    if(!Number.isFinite(n) || n <= 0)
      return jsonResponse({ok:false,error:`${key} CDS 必須為大於 0 的數值`},400);
    values[key] = round2(n);
  }
  if(!Object.keys(values).length)
    return jsonResponse({ok:false,error:'沒有可寫入的 CDS 數值'},400);

  const marketPath = `data/market_${reportDate}.json`;
  try{
    await readJson(env, marketPath, {allow404:false});
  }catch(e){
    return jsonResponse({ok:false,error:`找不到 ${reportDate} 的市場資料，未寫入 CDS：${e.message}`},404);
  }

  const updated = await updateJson(env, marketPath, data => {
    data.cds = data.cds || {};
    for(const [key,value] of Object.entries(values)){
      const prevFromBody = body.previous?.[key];
      const existing = data.cds?.[key];
      const prev = Number.isFinite(Number(prevFromBody))
        ? Number(prevFromBody)
        : (existing?.prev ?? null);

      data.cds[key] = {
        value,
        prev: prev == null ? null : Number(prev),
        chg_abs: prev == null ? null : round2(value - Number(prev)),
        chg_pct: prev == null || Number(prev) === 0
          ? null
          : round2((value - Number(prev)) / Number(prev) * 100),
        date: reportDate,
        name: CDS_META[key].name,
        flag: CDS_META[key].flag,
        source: 'manual'
      };
    }
    data.target_date = data.target_date || reportDate;
    data.cds_updated_at = new Date().toISOString();
    data.cds_updated_by = actor;
    return data;
  }, `Manual CDS update: ${reportDate} (${actor})`, {createIfMissing:false,retries:1});

  let warning = null;
  try{
    const latest = await readJson(env,'data/latest.json',{allow404:true});
    if(latest.exists && latest.data?.target_date === reportDate){
      await updateJson(env,'data/latest.json',data => {
        data.cds = updated.data.cds;
        data.cds_updated_at = updated.data.cds_updated_at;
        data.cds_updated_by = actor;
        return data;
      }, `Sync manual CDS to latest: ${reportDate} (${actor})`,
      {createIfMissing:false,retries:1});
    }
  }catch(e){
    warning = `market 檔已更新，但 latest 同步失敗：${e.message}`;
  }

  return jsonResponse({ok:true,data:updated.data,updated_by:actor,...(warning?{warning}:{})});
}

async function writeManual(body, env, actor){
  const reportDate = body.report_date;
  if(!validDate(reportDate))
    return jsonResponse({ok:false,error:'report_date 格式錯誤'},400);

  const raw = body.overrides && typeof body.overrides === 'object' ? body.overrides : {};
  const cleaned = {};

  for(const key of INDICATORS){
    const rec = cleanManualRecord(raw[key], reportDate, actor);
    if(rec) cleaned[key] = rec;
  }

  const stocks = {};
  for(const [symbol,rawRec] of Object.entries(raw.stocks || {})){
    if(!/^[A-Z0-9.\-]{1,15}$/.test(symbol)) continue;
    const rec = cleanManualRecord(rawRec, reportDate, actor);
    if(rec) stocks[symbol] = rec;
  }
  if(Object.keys(stocks).length) cleaned.stocks = stocks;

  const updated = await updateJson(env,'data/manual_override.json',store => {
    if(Object.keys(cleaned).length) store[reportDate] = cleaned;
    else delete store[reportDate];
    return store;
  }, `Manual market override: ${reportDate} (${actor})`,
  {createIfMissing:true,retries:1});

  return jsonResponse({
    ok:true, report_date:reportDate, overrides:cleaned,
    updated_by:actor, data:updated.data
  });
}

async function writeConfig(body, env, actor){
  const cfg = validateConfig(body.config);
  const updated = await updateJson(
    env,'config.json',() => cfg,`Update config (${actor})`,
    {createIfMissing:false,retries:1}
  );
  return jsonResponse({ok:true,updated_by:actor,data:updated.data});
}

export async function onRequestPost({request,env}){
  const methodErr = requirePost(request);
  if(methodErr) return methodErr;

  const authErr = requireAccess(request,env);
  if(authErr) return authErr;

  try{
    const body = await request.json();
    const actor = actorFromRequest(request);

    if(body.action === 'cds') return await writeCDS(body,env,actor);
    if(body.action === 'manual') return await writeManual(body,env,actor);
    if(body.action === 'config') return await writeConfig(body,env,actor);

    return jsonResponse({ok:false,error:'不允許的 action'},400);
  }catch(e){
    return jsonResponse({ok:false,error:e.message || '寫入失敗'},500);
  }
}

