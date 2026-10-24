#!/usr/bin/env python3
"""
每日國際重要新聞摘要（風險管理中心「今日市場報告」使用）。

流程：
1) 從多個新聞 RSS 收集過去 24 小時的標題（只取標題、連結、來源、時間）。
2) 去重後，把編號清單交給 Groq（openai/gpt-oss-120b）挑出 5–8 則對債市、股市、匯市
   影響最大的國際新聞，寫成繁體中文摘要。
3) 驗證：每則摘要必須引用清單中真實存在的編號，對不上的整則丟棄；
   原文連結一律由程式依編號帶入，不使用模型產生的網址。
4) 寫入 news/latest.json 與 news/world_news_YYYY-MM-DD.json（台北日期）。

與市場資料（fetch_market.py）完全分開：本程式失敗不影響 data/ 內任何檔案。
沒有 GROQ_API_KEY 或模型呼叫失敗時，仍寫出 status 說明原因、items 為空，
入口網站據此顯示「今日摘要未產生」，不會沿用舊摘要冒充今天的新聞。

用法：
  python fetch_news.py            # 正式執行
  python fetch_news.py --dry-run  # 只收集標題並列印，不呼叫模型、不寫檔
"""

import email.utils
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

NEWS_DIR = Path(__file__).parent / "news"
TPE = ZoneInfo("Asia/Taipei")
MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WINDOW_HOURS = 24
MAX_PER_FEED = 18
MAX_HEADLINES = 70          # Groq 免費方案每分鐘 8K tokens，標題數需控制
MIN_ITEMS, MAX_ITEMS = 5, 8
MARKETS = {"bond": "債市", "equity": "股市", "fx": "匯市", "macro": "總經"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*",
}

GN = "https://news.google.com/rss"
FEEDS = [
    ("Google News 商業", f"{GN}/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"),
    ("Google News 國際", f"{GN}/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en"),
    ("Google News 市場", f"{GN}/search?q=(stocks+OR+bonds+OR+%22Treasury+yields%22+OR+%22Federal+Reserve%22+OR+dollar)+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News 亞洲", f"{GN}/search?q=(%22Asian+markets%22+OR+Nikkei+OR+%22Hang+Seng%22+OR+yuan+OR+%22Bank+of+Japan%22+OR+Taiwan+dollar)+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News 台灣財經", f"{GN}/headlines/section/topic/BUSINESS?hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
]


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_time(value):
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(xml_bytes, feed_name):
    """支援 RSS 2.0 與 Atom。回傳 [{title, url, source, published}]。"""
    root = ET.fromstring(xml_bytes)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for item in root.iter("item"):
        title = clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        src_el = item.find("source")
        source = clean(src_el.text) if src_el is not None and src_el.text else feed_name
        # Google News 標題結尾常帶「 - 媒體名稱」，拆開以免重複
        if src_el is not None and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].strip()
        out.append({"title": title, "url": link, "source": source,
                    "published": parse_time(item.findtext("pubDate"))})
    for entry in root.iter(f"{atom}entry"):
        link_el = entry.find(f"{atom}link")
        out.append({
            "title": clean(entry.findtext(f"{atom}title")),
            "url": link_el.get("href", "") if link_el is not None else "",
            "source": feed_name,
            "published": parse_time(entry.findtext(f"{atom}updated") or entry.findtext(f"{atom}published")),
        })
    return [x for x in out if x["title"] and x["url"].startswith("http")]


def collect(now_utc):
    cutoff = now_utc - timedelta(hours=WINDOW_HOURS)
    per_feed, status = [], []
    for name, url in FEEDS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            items = parse_feed(r.content, name)
            fresh = [x for x in items if x["published"] and cutoff <= x["published"] <= now_utc + timedelta(hours=1)]
            fresh.sort(key=lambda x: x["published"], reverse=True)
            per_feed.append(fresh[:MAX_PER_FEED])
            status.append({"name": name, "ok": True, "count": len(fresh)})
            print(f"  ✅ {name}: {len(items)} 則，過去 {WINDOW_HOURS} 小時 {len(fresh)} 則")
        except Exception as exc:
            per_feed.append([])
            status.append({"name": name, "ok": False, "count": 0, "error": f"{type(exc).__name__}"})
            print(f"  ⚠ {name}: {type(exc).__name__}: {exc}")

    # 依來源輪流挑選，避免單一來源占滿；以正規化標題去重
    seen, picked = set(), []
    depth = max((len(f) for f in per_feed), default=0)
    for i in range(depth):
        for feed in per_feed:
            if i < len(feed) and len(picked) < MAX_HEADLINES:
                key = re.sub(r"[^\w]+", "", feed[i]["title"].lower())[:80]
                if key and key not in seen:
                    seen.add(key)
                    picked.append(feed[i])
    for idx, h in enumerate(picked, 1):
        h["id"] = idx
    return picked, status


SYSTEM_PROMPT = (
    "你是台灣一家證券公司風險管理部的市場助理。使用者會給你一份編號的國際新聞標題清單。"
    "請只根據清單中的標題內容判斷，不要加入清單沒有的事實、數字或推測。"
    f"挑出 {MIN_ITEMS} 到 {MAX_ITEMS} 則對全球債市、股市、匯市影響最大的事件；"
    "同一事件的多則標題請合併成一則並列出所有相關編號。"
    "優先順序：央行與利率、重大經濟數據、地緣政治與政策、大型企業或金融機構信用事件、主要貨幣大幅波動。"
    "略過純個股財報細節、生活、體育、娛樂新聞。"
    "輸出 JSON 物件，格式為 {\"items\": [{\"ids\": [編號], \"market\": \"bond|equity|fx|macro\", "
    "\"title\": \"繁體中文標題，20 字以內\", \"summary\": \"繁體中文摘要，1 到 2 句，60 字以內，只陳述標題中的事實\"}]}。"
    "依重要性由高到低排列。只輸出 JSON。"
)


def call_groq(headlines, api_key):
    lines = "\n".join(f"[{h['id']}] {h['title']}（{h['source']}）" for h in headlines)
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "新聞標題清單：\n" + lines},
        ],
        "temperature": 0.2,
        "max_completion_tokens": 2500,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    r = requests.post(GROQ_URL, json=body, timeout=90,
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:300]}")
    payload = r.json()
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage", {})
    return content, usage


def validate(content, headlines):
    """只保留引用真實編號的項目；連結由程式依編號帶入。"""
    by_id = {h["id"]: h for h in headlines}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content or "", re.S)
        data = json.loads(match.group(0)) if match else {}
    items, used = [], set()
    for raw in (data.get("items") or [])[: MAX_ITEMS * 2]:
        if not isinstance(raw, dict):
            continue
        ids = []
        for v in raw.get("ids") or []:
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v in by_id and v not in ids:
                ids.append(v)
        title = clean(str(raw.get("title") or ""))[:40]
        summary = clean(str(raw.get("summary") or ""))[:140]
        market = raw.get("market") if raw.get("market") in MARKETS else "macro"
        if not ids or not title or not summary or used.intersection(ids):
            continue
        used.update(ids)
        items.append({
            "market": market,
            "market_name": MARKETS[market],
            "title": title,
            "summary": summary,
            "sources": [{
                "title": by_id[i]["title"],
                "url": by_id[i]["url"],
                "source": by_id[i]["source"],
                "published": by_id[i]["published"].isoformat(),
            } for i in ids[:3]],
        })
        if len(items) >= MAX_ITEMS:
            break
    return items


def write(result):
    NEWS_DIR.mkdir(exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    (NEWS_DIR / f"world_news_{result['date']}.json").write_text(text, encoding="utf-8")
    (NEWS_DIR / "latest.json").write_text(text, encoding="utf-8")
    print(f"✅ 已寫入 news/world_news_{result['date']}.json 與 news/latest.json")


def main():
    dry = "--dry-run" in sys.argv
    now_utc = datetime.now(timezone.utc)
    print("🕒 Taiwan time:", now_utc.astimezone(TPE).isoformat())
    print("📡 收集新聞標題")
    headlines, feeds = collect(now_utc)
    print(f"📰 去重後 {len(headlines)} 則")
    result = {
        "generated_at": now_utc.isoformat(),
        "date": now_utc.astimezone(TPE).strftime("%Y-%m-%d"),
        "window_hours": WINDOW_HOURS,
        "model": MODEL,
        "headline_count": len(headlines),
        "feeds": feeds,
        "status": "ok",
        "note": "",
        "items": [],
    }
    if dry:
        for h in headlines:
            print(f"  [{h['id']}] {h['published'].astimezone(TPE):%m/%d %H:%M} {h['title']}（{h['source']}）")
        print("ℹ️ dry run：不呼叫模型、不寫檔")
        return
    if len(headlines) < MIN_ITEMS:
        result.update(status="no_headlines", note="新聞來源取得的標題太少，今日不產生摘要")
        write(result)
        return
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        result.update(status="no_key", note="尚未設定 GROQ_API_KEY，今日不產生摘要")
        write(result)
        return
    try:
        content, usage = call_groq(headlines, api_key)
        items = validate(content, headlines)
        result["usage"] = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    except Exception as exc:
        print(f"❌ 模型呼叫失敗：{type(exc).__name__}: {exc}")
        result.update(status="llm_error", note="模型呼叫失敗，今日不產生摘要")
        write(result)
        return
    if not items:
        result.update(status="llm_error", note="模型回傳內容無法對應到新聞來源，今日不產生摘要")
    result["items"] = items
    print(f"🧾 有效摘要 {len(items)} 則")
    for it in items:
        print(f"  [{it['market_name']}] {it['title']}：{it['summary']}")
    write(result)


if __name__ == "__main__":
    main()
