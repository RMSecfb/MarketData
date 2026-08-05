#!/usr/bin/env python3
"""
金融市場每日監控指標 — 資料抓取腳本
GitHub Actions 每日自動執行，輸出 data/YYYY-MM-DD.json
手動補抓：python fetch_market.py 2026-03-07
"""

import json
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

import yfinance as yf
import pandas as pd
import requests

# ── CONFIG ──────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# 手動指定日期（補抓歷史）
if len(sys.argv) > 1:
    TARGET = sys.argv[1]  # "2026-03-07"
else:
    # Actions 在 UTC 22:30 執行（美股已收盤），date.today() 即為當天交易日
    d = date.today()
    while d.weekday() >= 5:   # 保險：萬一排程意外在週末跑，往回找最近交易日
        d -= timedelta(days=1)
    TARGET = d.strftime("%Y-%m-%d")

print(f"🗓  Target date: {TARGET}")


# ── HELPERS ─────────────────────────────────────────────────
def make_entry(curr_val, prev_val, curr_date):
    if curr_val is None:
        return None
    chg_abs = round(curr_val - prev_val, 6) if prev_val is not None else None
    chg_pct = round((curr_val - prev_val) / prev_val * 100, 4) if prev_val is not None else None
    return {
        "value":   round(curr_val, 4),
        "prev":    round(prev_val, 4) if prev_val is not None else None,
        "chg_abs": chg_abs,
        "chg_pct": chg_pct,
        "date":    curr_date,
    }


def treasury_get(target_date_str):
    """從 Treasury.gov 官方 API 抓 2Y/10Y/30Y 殖利率
    XML 結構：feed > entry > content > m:properties > d:BC_2YEAR ...
    """
    import xml.etree.ElementTree as ET
    d = datetime.strptime(target_date_str, "%Y-%m-%d")
    months = set()
    for delta in [0, 1, 2]:
        m = d - timedelta(days=30*delta)
        months.add(m.strftime("%Y%m"))

    D    = 'http://schemas.microsoft.com/ado/2007/08/dataservices'
    M    = 'http://schemas.microsoft.com/ado/2007/08/dataservices/metadata'
    rows = []

    for ym in sorted(months, reverse=True):
        url = (f"https://home.treasury.gov/resource-center/data-chart-center"
               f"/interest-rates/pages/xml?data=daily_treasury_yield_curve"
               f"&field_tdr_date_value_month={ym}")
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            count = 0
            # properties 在 entry > content > m:properties，用 iter 直接找
            for props in root.iter(f'{{{M}}}properties'):
                date_el = props.find(f'{{{D}}}NEW_DATE')
                y2_el   = props.find(f'{{{D}}}BC_2YEAR')
                y10_el  = props.find(f'{{{D}}}BC_10YEAR')
                y30_el  = props.find(f'{{{D}}}BC_30YEAR')
                if date_el is None or not date_el.text:
                    continue
                date_only = date_el.text[:10]  # "2026-03-09T00:00:00" -> "2026-03-09"
                if date_only > target_date_str:
                    continue
                y2v  = float(y2_el.text)  if y2_el  is not None and y2_el.text  else None
                y10v = float(y10_el.text) if y10_el is not None and y10_el.text else None
                y30v = float(y30_el.text) if y30_el is not None and y30_el.text else None
                if y2v is not None:
                    rows.append((date_only, y2v, y10v, y30v))
                    count += 1
            print(f"  📥 Treasury.gov {ym}: {count} 筆")
        except Exception as e:
            print(f"  ❌ Treasury.gov {ym}: {type(e).__name__}: {e}")

    if not rows:
        print("  ⚠️  Treasury.gov 無有效資料")
        return None, None, None

    rows.sort(key=lambda x: x[0], reverse=True)
    curr = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    print(f"  ✅ curr={curr[0]} 2Y={curr[1]} 10Y={curr[2]} 30Y={curr[3]}")

    def mk(idx):
        cv = curr[idx]
        pv = prev[idx] if prev else None
        return make_entry(cv, pv, curr[0]) if cv is not None else None

    return mk(1), mk(2), mk(3)


def yf_get(symbol, target_date_str):
    """用 yfinance 抓股票/指數，找最接近 target_date 的收盤價"""
    try:
        d = datetime.strptime(target_date_str, "%Y-%m-%d")
        start = (d - timedelta(days=14)).strftime("%Y-%m-%d")
        end   = (d + timedelta(days=2)).strftime("%Y-%m-%d")
        t = yf.Ticker(symbol)
        hist = t.history(start=start, end=end, interval="1d", auto_adjust=True)
        if hist.empty:
            raise ValueError("empty")
        # 只取 <= target_date 的列
        hist.index = hist.index.strftime("%Y-%m-%d")
        valid = hist[hist.index <= target_date_str].sort_index(ascending=False)
        if valid.empty:
            raise ValueError("no valid rows")
        curr_date = valid.index[0]
        curr_val  = float(valid.iloc[0]["Close"])
        prev_val  = float(valid.iloc[1]["Close"]) if len(valid) > 1 else None
        return make_entry(curr_val, prev_val, curr_date)
    except Exception as e:
        # fallback: yf.download（對 ^MOVE 等特殊 ticker 較穩定）
        print(f"  ⚠ t.history failed ({e}), trying yf.download...")
        try:
            hist2 = yf.download(symbol, start=start, end=end, interval="1d",
                                auto_adjust=True, progress=False)
            if hist2.empty:
                print(f"  ❌ yfinance {symbol}: no data")
                return None
            # MultiIndex 欄位攤平
            if isinstance(hist2.columns, pd.MultiIndex):
                hist2.columns = hist2.columns.get_level_values(0)
            hist2.index = hist2.index.strftime("%Y-%m-%d")
            valid2 = hist2[hist2.index <= target_date_str].sort_index(ascending=False)
            if valid2.empty:
                print(f"  ❌ yfinance {symbol}: no valid rows after filter")
                return None
            curr_date = valid2.index[0]
            curr_val  = float(valid2.iloc[0]["Close"])
            prev_val  = float(valid2.iloc[1]["Close"]) if len(valid2) > 1 else None
            return make_entry(curr_val, prev_val, curr_date)
        except Exception as e2:
            print(f"  ❌ yfinance {symbol}: {e2}")
            return None


def move_get(target_date_str):
    """抓 MOVE 指數：
    - 若 target 是今日 → 用 quoteSummary API 抓當日最新報價（不 lag）
    - 否則 → 用 chart API 抓歷史資料
    - 以上失敗 → fallback yf_get
    """
    today_str = date.today().strftime("%Y-%m-%d")

    # 方法1：target 是今日，用 quoteSummary 抓最新報價
    if target_date_str == today_str:
        for host in ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]:
            try:
                url = f"https://{host}/v10/finance/quoteSummary/%5EMOVE"
                params = {"modules": "price"}
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Referer": "https://finance.yahoo.com/",
                }
                r = requests.get(url, params=params, headers=headers, timeout=15)
                r.raise_for_status()
                j = r.json()
                price = j["quoteSummary"]["result"][0]["price"]
                curr_val  = price["regularMarketPrice"]["raw"]
                prev_val  = price["regularMarketPreviousClose"]["raw"]
                market_ts = price["regularMarketTime"]["raw"]
                curr_date = datetime.utcfromtimestamp(market_ts).strftime("%Y-%m-%d")
                print(f"  ✅ MOVE (quote {host}): {curr_val:.2f} ({curr_date})")
                return make_entry(curr_val, prev_val, curr_date)
            except Exception as e:
                print(f"  ⚠ MOVE quote {host}: {e}")

    # 方法2：chart API 抓歷史資料
    for host in ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]:
        try:
            url = f"https://{host}/v8/finance/chart/%5EMOVE"
            params = {"interval": "1d", "range": "10d"}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Referer": "https://finance.yahoo.com/",
            }
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            j = r.json()
            ts    = j["chart"]["result"][0]["timestamp"]
            close = j["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            rows = []
            for t, c in zip(ts, close):
                if c is None: continue
                dt_str = datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
                rows.append((dt_str, c))
            rows.sort(reverse=True)
            valid = [(dt, v) for dt, v in rows if dt <= target_date_str]
            if valid:
                curr_date, curr_val = valid[0]
                prev_val = valid[1][1] if len(valid) > 1 else None
                print(f"  ✅ MOVE (chart {host}): {curr_val:.2f} ({curr_date})")
                return make_entry(curr_val, prev_val, curr_date)
        except Exception as e:
            print(f"  ⚠ MOVE chart {host}: {e}")

    # 方法3：fallback yf_get
    print(f"  ⚠ MOVE fallback yf_get...")
    result = yf_get("^MOVE", target_date_str)
    if result:
        print(f"  ✅ MOVE (yf_get): {result['value']} ({result['date']})")
    else:
        print(f"  ❌ MOVE: 所有來源都失敗")
    return result


# ── FETCH ALL ────────────────────────────────────────────────
def fetch_all(target):
    result = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "target_date":  target,
    }
    errors = []

    # VIX — yfinance ^VIX（與 MOVE 同步，無 FRED 延遲問題）
    print("📡 VIX...")
    d = yf_get("^VIX", target)
    result["vix"] = d
    print(f"  {'✅' if d else '❌'} VIX: {d['value'] if d else 'N/A'}")

    # MOVE — 同時查 Yahoo Finance API 和 FRED，選日期較新的那筆
    print("📡 MOVE...")
    d = move_get(target)
    result["move"] = d
    print(f"  {'✅' if d else '❌'} MOVE: {d['value'] if d else 'N/A'}")

    # 公債殖利率 — Treasury.gov 官方 API（無延遲、無需 API key）
    print("📡 2Y/10Y/30Y 公債（Treasury.gov）...")
    y2, y10, y30 = treasury_get(target)
    result["y2"]  = y2
    result["y10"] = y10
    result["y30"] = y30
    print(f"  {'✅' if y2  else '❌'} 2Y:  {y2['value']  if y2  else 'N/A'}%")
    print(f"  {'✅' if y10 else '❌'} 10Y: {y10['value'] if y10 else 'N/A'}%")
    print(f"  {'✅' if y30 else '❌'} 30Y: {y30['value'] if y30 else 'N/A'}%")

    # 10Y-2Y 利差（直接計算）
    if result.get("y10") and result.get("y2"):
        spread = round((result["y10"]["value"] - result["y2"]["value"]) * 100, 2)
        result["spread"] = spread
        print(f"  ✅ 10Y-2Y 利差: {spread} bps")
    else:
        result["spread"] = None

    # SOX — yfinance ^SOX
    print("📡 SOX...")
    d = yf_get("^SOX", target)
    result["sox"] = d
    print(f"  {'✅' if d else '❌'} SOX: {d['value'] if d else 'N/A'}")

    # 個股
    stocks_meta = {
        "NVDA": {"name": "輝達",     "emoji": "🟢", "grade": "IG1"},
        "TSM":  {"name": "台積電",   "emoji": "🔵", "grade": "IG1"},
        "SMCI": {"name": "超微電腦", "emoji": "⚡",  "grade": "HY1"},
        "ARM":  {"name": "安謀控股", "emoji": "💻", "grade": "IG1"},
        "TSLA": {"name": "特斯拉",   "emoji": "🚗", "grade": "IG3"},
    }
    result["stocks"] = {}
    for sym, meta in stocks_meta.items():
        print(f"📡 {sym}...")
        d = yf_get(sym, target)
        if d:
            d.update(meta)
        result["stocks"][sym] = d
        print(f"  {'✅' if d else '❌'} {sym}: ${d['value'] if d else 'N/A'}")

    if errors:
        result["errors"] = errors

    return result


# ── MAIN ─────────────────────────────────────────────────────
if __name__ == "__main__":
    data = fetch_all(TARGET)

    # 存成日期檔
    out_path = DATA_DIR / f"market_{TARGET}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已寫入 {out_path}")

    # 同時更新 latest.json（給 HTML 今日頁面用）
    latest_path = DATA_DIR / "latest.json"
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 已更新 {latest_path}")

    # 輸出摘要
    print("\n📊 數據摘要:")
    if data.get("vix"):
        v = data["vix"]["value"]
        print(f"  VIX: {v:.2f} {'🚨 恐慌' if v>=30 else '⚠️ 警戒' if v>=20 else '✅ 正常'}")
    if data.get("y10") and data.get("y2"):
        print(f"  10Y-2Y 利差: {data['spread']} bps {'🚨 倒掛！' if data['spread']<0 else ''}")
    for sym, s in data.get("stocks", {}).items():
        if s:
            print(f"  {sym}: ${s['value']:.2f} ({s['chg_pct']:+.2f}%)")

