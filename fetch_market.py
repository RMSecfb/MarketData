#!/usr/bin/env python3
"""
Market data fetcher for GitHub Actions.

Rules:
- Prefer confirmed TARGET-date data.
- If TARGET is unavailable for one item, use the nearest confirmed prior date <= TARGET.
- Stale data does not fail the whole batch.
- Reject None / NaN / inf and future-dated values.
- Preserve existing manually maintained CDS when rewriting the same report date.
- Historical backfill must not rewind latest.json.

Fetch order for Yahoo symbols:
1) Yahoo daily chart API (TARGET)
2) Yahoo intraday 5m chart API (TARGET)
3) yfinance Ticker.history (TARGET)
4) yf.download (TARGET)
5) nearest confirmed prior daily value

Treasury yields:
- Treasury.gov official XML; latest confirmed row <= TARGET

Extended indicators (markets_ext):
- Asia/US equity indices, FX and US high-yield OAS for the Risk Intelligence Center.
- Stored under data["markets_ext"] with their own quality block.
- NOT part of checks_for()/validate_data(): a missing or stale extended item
  never blocks writing the day's JSON and never triggers the 06:30/07:00 retry.
"""

import json
import math
import re
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
import requests
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://finance.yahoo.com/",
}
STOCKS_META = {
    "NVDA": {"name": "輝達", "emoji": "🟢", "grade": "IG1"},
    "TSM": {"name": "台積電", "emoji": "🔵", "grade": "IG1"},
    "SMCI": {"name": "超微電腦", "emoji": "⚡", "grade": "HY1"},
    "ARM": {"name": "安謀控股", "emoji": "💻", "grade": "IG1"},
    "TSLA": {"name": "特斯拉", "emoji": "🚗", "grade": "IG3"},
}

def get_target_date():
    if len(sys.argv) > 1:
        target = sys.argv[1].strip()
        datetime.strptime(target, "%Y-%m-%d")
        return target
    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    nyse = mcal.get_calendar("NYSE")
    schedule = nyse.schedule(
        start_date=now_tw.date() - timedelta(days=14),
        end_date=now_tw.date() - timedelta(days=1),
    )
    if schedule.empty:
        raise RuntimeError("找不到最近的 NYSE 交易日")
    return schedule.index[-1].date().strftime("%Y-%m-%d")

TARGET = get_target_date()

def is_valid_number(value):
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False

def make_entry(curr_val, prev_val, curr_date, source, target_date):
    if not is_valid_number(curr_val):
        return None
    curr_val = float(curr_val)
    prev_val = float(prev_val) if is_valid_number(prev_val) else None
    chg_abs = round(curr_val - prev_val, 6) if prev_val is not None else None
    chg_pct = (
        round((curr_val - prev_val) / prev_val * 100, 4)
        if prev_val not in (None, 0)
        else None
    )
    return {
        "value": round(curr_val, 4),
        "prev": round(prev_val, 4) if prev_val is not None else None,
        "chg_abs": chg_abs,
        "chg_pct": chg_pct,
        "date": curr_date,
        "target_date": target_date,
        "is_stale": curr_date != target_date,
        "source": source,
    }

def market_date_from_timestamp(timestamp, timezone_name):
    return datetime.fromtimestamp(
        timestamp, ZoneInfo(timezone_name or "UTC")
    ).strftime("%Y-%m-%d")

def yahoo_chart_request(symbol, params):
    encoded = quote(symbol, safe="")
    for host in YAHOO_HOSTS:
        try:
            response = requests.get(
                f"https://{host}/v8/finance/chart/{encoded}",
                params=params,
                headers=HEADERS,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            chart = payload.get("chart", {})
            if chart.get("error"):
                raise RuntimeError(str(chart["error"]))
            results = chart.get("result") or []
            if not results:
                raise ValueError("Yahoo chart result empty")
            return results[0], host
        except Exception as exc:
            print(f"  ⚠ {symbol} {host}: {type(exc).__name__}: {exc}")
    return None, None

def yahoo_daily_get(symbol, target_date, allow_stale=False):
    result, host = yahoo_chart_request(
        symbol,
        {
            "interval": "1d",
            "range": "1mo",
            "includePrePost": "false",
            "events": "div,splits",
        },
    )
    if result is None:
        return None
    timestamps = result.get("timestamp") or []
    quotes = result.get("indicators", {}).get("quote") or []
    if not timestamps or not quotes:
        return None
    closes = quotes[0].get("close") or []
    tz_name = result.get("meta", {}).get("exchangeTimezoneName")
    rows = []
    for ts, close in zip(timestamps, closes):
        if not is_valid_number(close):
            continue
        trade_date = market_date_from_timestamp(ts, tz_name)
        if trade_date <= target_date:
            rows.append((trade_date, float(close)))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0], reverse=True)
    curr_date, curr_val = rows[0]
    prev_val = rows[1][1] if len(rows) > 1 else None
    if curr_date != target_date and not allow_stale:
        print(f"  ⚠ {symbol} daily stale: latest={curr_date}, target={target_date}")
        return None
    entry = make_entry(
        curr_val, prev_val, curr_date, f"yahoo_daily:{host}", target_date
    )
    icon = "⚠" if entry["is_stale"] else "✅"
    print(f"  {icon} {symbol} Yahoo daily: {entry['value']} ({entry['date']})")
    return entry

def yahoo_previous_close(symbol, target_date):
    result, _ = yahoo_chart_request(
        symbol,
        {"interval": "1d", "range": "1mo", "includePrePost": "false"},
    )
    if result is None:
        return None
    timestamps = result.get("timestamp") or []
    quotes = result.get("indicators", {}).get("quote") or []
    if not quotes:
        return None
    closes = quotes[0].get("close") or []
    tz_name = result.get("meta", {}).get("exchangeTimezoneName")
    rows = []
    for ts, close in zip(timestamps, closes):
        if not is_valid_number(close):
            continue
        trade_date = market_date_from_timestamp(ts, tz_name)
        if trade_date < target_date:
            rows.append((trade_date, float(close)))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[0][1] if rows else None

def yahoo_intraday_get(symbol, target_date):
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    ny_tz = ZoneInfo("America/New_York")
    start_dt = datetime.combine(target, dt_time(0, 0), tzinfo=ny_tz)
    end_dt = start_dt + timedelta(days=1)
    result, host = yahoo_chart_request(
        symbol,
        {
            "period1": int(start_dt.timestamp()),
            "period2": int(end_dt.timestamp()),
            "interval": "5m",
            "includePrePost": "false",
            "events": "div,splits",
        },
    )
    if result is None:
        return None
    timestamps = result.get("timestamp") or []
    quotes = result.get("indicators", {}).get("quote") or []
    if not timestamps or not quotes:
        return None
    closes = quotes[0].get("close") or []
    tz_name = (
        result.get("meta", {}).get("exchangeTimezoneName")
        or "America/New_York"
    )
    market_tz = ZoneInfo(tz_name)
    rows = []
    for ts, close in zip(timestamps, closes):
        if not is_valid_number(close):
            continue
        local_dt = datetime.fromtimestamp(ts, market_tz)
        if local_dt.strftime("%Y-%m-%d") != target_date:
            continue
        if not (dt_time(9, 30) <= local_dt.time() <= dt_time(16, 5)):
            continue
        rows.append((local_dt, float(close)))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    curr_dt, curr_val = rows[-1]
    entry = make_entry(
        curr_val,
        yahoo_previous_close(symbol, target_date),
        target_date,
        f"yahoo_intraday_5m:{host}",
        target_date,
    )
    print(
        f"  ✅ {symbol} Yahoo intraday: {entry['value']} "
        f"({entry['date']}) last_bar={curr_dt.strftime('%H:%M')}"
    )
    return entry

def yfinance_history_get(symbol, target_date, allow_stale=False):
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    start = (target_dt - timedelta(days=14)).strftime("%Y-%m-%d")
    end = (target_dt + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        hist = yf.Ticker(symbol).history(
            start=start, end=end, interval="1d", auto_adjust=True
        )
        if hist.empty:
            raise ValueError("empty")
        hist.index = hist.index.strftime("%Y-%m-%d")
        valid = hist[hist.index <= target_date].sort_index(ascending=False)
        if valid.empty:
            raise ValueError("no valid rows")
        curr_date = valid.index[0]
        if curr_date != target_date and not allow_stale:
            raise ValueError(f"stale date {curr_date}")
        entry = make_entry(
            valid.iloc[0]["Close"],
            valid.iloc[1]["Close"] if len(valid) > 1 else None,
            curr_date,
            "yfinance_history",
            target_date,
        )
        if entry is None:
            raise ValueError("invalid current value")
        return entry
    except Exception as exc:
        print(f"  ⚠ {symbol} yfinance history: {type(exc).__name__}: {exc}")
        return None

def yf_download_get(symbol, target_date, allow_stale=False):
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    start = (target_dt - timedelta(days=14)).strftime("%Y-%m-%d")
    end = (target_dt + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        hist = yf.download(
            symbol,
            start=start,
            end=end,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if hist.empty:
            raise ValueError("empty")
        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)
        hist.index = hist.index.strftime("%Y-%m-%d")
        valid = hist[hist.index <= target_date].sort_index(ascending=False)
        if valid.empty:
            raise ValueError("no valid rows")
        curr_date = valid.index[0]
        if curr_date != target_date and not allow_stale:
            raise ValueError(f"stale date {curr_date}")
        entry = make_entry(
            valid.iloc[0]["Close"],
            valid.iloc[1]["Close"] if len(valid) > 1 else None,
            curr_date,
            "yf_download",
            target_date,
        )
        if entry is None:
            raise ValueError("invalid current value")
        return entry
    except Exception as exc:
        print(f"  ⚠ {symbol} yf.download: {type(exc).__name__}: {exc}")
        return None

def market_get(symbol, target_date):
    for label, getter in (
        ("Yahoo daily", yahoo_daily_get),
        ("Yahoo intraday 5m", yahoo_intraday_get),
        ("yfinance history", yfinance_history_get),
        ("yf.download", yf_download_get),
    ):
        if label != "Yahoo daily":
            print(f"  ↪ {symbol}: fallback → {label}")
        result = getter(symbol, target_date)
        if result:
            return result

    print(f"  ↪ {symbol}: TARGET unavailable → nearest confirmed prior date")
    for getter in (yahoo_daily_get, yfinance_history_get, yf_download_get):
        result = getter(symbol, target_date, allow_stale=True)
        if result:
            result["fallback_reason"] = "target_date_unavailable"
            return result
    return None

def treasury_get(target_date):
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    months = {
        target_dt.strftime("%Y%m"),
        (target_dt - timedelta(days=31)).strftime("%Y%m"),
    }
    d_ns = "http://schemas.microsoft.com/ado/2007/08/dataservices"
    m_ns = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
    rows = []
    for ym in sorted(months, reverse=True):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/pages/xml?data=daily_treasury_yield_curve"
            f"&field_tdr_date_value_month={ym}"
        )
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            count = 0
            for props in root.iter(f"{{{m_ns}}}properties"):
                date_el = props.find(f"{{{d_ns}}}NEW_DATE")
                y2_el = props.find(f"{{{d_ns}}}BC_2YEAR")
                y10_el = props.find(f"{{{d_ns}}}BC_10YEAR")
                y30_el = props.find(f"{{{d_ns}}}BC_30YEAR")
                if date_el is None or not date_el.text:
                    continue
                trade_date = date_el.text[:10]
                if trade_date > target_date:
                    continue
                vals = []
                for el in (y2_el, y10_el, y30_el):
                    vals.append(float(el.text) if el is not None and el.text else None)
                if all(is_valid_number(v) for v in vals):
                    rows.append((trade_date, *vals))
                    count += 1
            print(f"  📥 Treasury.gov {ym}: {count} 筆")
        except Exception as exc:
            print(f"  ❌ Treasury.gov {ym}: {type(exc).__name__}: {exc}")
    if not rows:
        return None, None, None
    rows = sorted(set(rows), key=lambda x: x[0], reverse=True)
    curr = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    def build(idx):
        entry = make_entry(
            curr[idx], prev[idx] if prev else None, curr[0],
            "treasury_gov", target_date
        )
        if entry and entry["is_stale"]:
            entry["fallback_reason"] = "target_date_unavailable"
        return entry
    return build(1), build(2), build(3)

# ── 延伸指標（markets_ext）────────────────────────────────
# 供風險管理中心大螢幕使用。與主要監控項目分開：抓不到只標示該項，
# 不影響主要資料寫入，也不納入 data_quality 與排程重抓判斷。
EXT_SYMBOLS = {
    "indices": [
        ("twii", "^TWII", "台股加權"),
        ("n225", "^N225", "日經 225"),
        ("kospi", "^KS11", "韓國 KOSPI"),
        ("hsi", "^HSI", "香港恒生"),
        ("sse", "000001.SS", "上海綜合"),
        ("spx", "^GSPC", "S&P 500"),
        ("ixic", "^IXIC", "NASDAQ 綜合"),
    ],
    "fx": [
        ("usdtwd", "TWD=X", "美元兌台幣"),
        ("usdhkd", "HKD=X", "美元兌港幣"),
        ("usdjpy", "JPY=X", "美元兌日圓"),
        ("audusd", "AUDUSD=X", "澳幣兌美元"),
    ],
}
FRED_SERIES = {
    "hy_oas": ("BAMLH0A0HYM2", "美國高收益債利差"),
}

def yahoo_meta_get(symbol, target_date):
    """Yahoo 日線最新一根的收盤價有時是空值（非美國指數常見），真正的收盤放在 meta 的
    regularMarketPrice／regularMarketTime。只在 meta 的市場日期等於目標日時採用，
    前一日收盤取日線中早於目標日的最後一筆。"""
    result, host = yahoo_chart_request(
        symbol, {"interval": "1d", "range": "1mo", "includePrePost": "false"}
    )
    if result is None:
        return None
    meta = result.get("meta", {}) or {}
    tz_name = meta.get("exchangeTimezoneName")
    timestamps = result.get("timestamp") or []
    quotes = result.get("indicators", {}).get("quote") or [{}]
    closes = quotes[0].get("close") or []
    tail = [
        (market_date_from_timestamp(ts, tz_name), c)
        for ts, c in list(zip(timestamps, closes))[-3:]
    ]
    price, mtime = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    mdate = market_date_from_timestamp(mtime, tz_name) if mtime else None
    print(f"  🔎 {symbol} 日線最後 3 筆 {tail}；meta 價格 {price}（{mdate}）")
    if not is_valid_number(price) or mdate != target_date:
        return None
    prev_rows = sorted(
        (
            (market_date_from_timestamp(ts, tz_name), float(c))
            for ts, c in zip(timestamps, closes)
            if is_valid_number(c)
        ),
        reverse=True,
    )
    prev_val = next((v for d, v in prev_rows if d < target_date), None)
    entry = make_entry(price, prev_val, mdate, f"yahoo_meta:{host}", target_date)
    if entry:
        print(f"  ✅ {symbol} Yahoo meta: {entry['value']} ({entry['date']})")
    return entry

TWSE_INDEX_URLS = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
    "https://www.twse.com.tw/exchangeReport/MI_INDEX",
)

def twse_taiex_on(day):
    """證交所每日收盤行情：回傳該日發行量加權股價指數的 (收盤, 前一日收盤)，休市回傳 None。"""
    ymd = day.replace("-", "")
    for url in TWSE_INDEX_URLS:
        try:
            response = requests.get(
                url,
                params={"date": ymd, "type": "IND", "response": "json"},
                headers=HEADERS,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            print(f"  ⚠ TWSE {day}: {type(exc).__name__}: {exc}")
            continue
        tables = [t.get("data") or [] for t in payload.get("tables") or []]
        tables += [v for k, v in payload.items() if k.startswith("data") and isinstance(v, list)]
        for rows in tables:
            for row in rows:
                if not row or "發行量加權股價指數" not in str(row[0]):
                    continue
                try:
                    close = float(str(row[1]).replace(",", ""))
                    points = float(str(row[3]).replace(",", ""))
                except (ValueError, IndexError):
                    continue
                sign = -1 if "-" in re.sub(r"<[^>]+>", "", str(row[2])) or "green" in str(row[2]) else 1
                return close, round(close - sign * points, 2)
        return None  # 有回應但沒有指數資料：休市
    return None

def twse_taiex_get(target_date, allow_stale=False):
    """台股加權指數改用證交所官方資料（Yahoo ^TWII 曾長時間停在舊日期）。"""
    day = datetime.strptime(target_date, "%Y-%m-%d")
    for back in range(0, 8 if allow_stale else 1):
        d = (day - timedelta(days=back)).strftime("%Y-%m-%d")
        if (day - timedelta(days=back)).weekday() >= 5:
            continue
        got = twse_taiex_on(d)
        time.sleep(0.5)
        if got:
            entry = make_entry(got[0], got[1], d, "twse_mi_index", target_date)
            if entry:
                print(f"  {'⚠' if entry['is_stale'] else '✅'} TWSE 加權指數: {entry['value']} ({d})")
            return entry
    return None

def ext_market_get(symbol, target_date):
    """延伸指標只用日線來源；美股盤中 5 分鐘線的交易時段不適用亞洲指數與外匯。
    順序：同日日線 → Yahoo meta 最新收盤 → 最近一筆（標示非同日）。台股加權以證交所為主。"""
    strict = [yahoo_daily_get, yfinance_history_get]
    if symbol == "^TWII":
        strict = [lambda s, t, **k: twse_taiex_get(t, **k)] + strict
    for getter in strict:
        result = getter(symbol, target_date)
        if result:
            return result
    result = yahoo_meta_get(symbol, target_date)
    if result:
        return result
    stale = [yahoo_daily_get, yfinance_history_get]
    if symbol == "^TWII":
        stale = [lambda s, t, **k: twse_taiex_get(t, **k)] + stale
    for getter in stale:
        result = getter(symbol, target_date, allow_stale=True)
        if result:
            result["fallback_reason"] = "target_date_unavailable"
            return result
    return None

def fred_get(series_id, target_date):
    """FRED 序列（百分比）轉為 bp。有 FRED_KEY 用官方 API，否則用公開 CSV。"""
    rows = []
    key = os.environ.get("FRED_KEY", "").strip()
    try:
        if key:
            response = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 15,
                    "observation_end": target_date,
                },
                timeout=20,
            )
            response.raise_for_status()
            for obs in response.json().get("observations", []):
                if is_valid_number(obs.get("value")):
                    rows.append((obs["date"], float(obs["value"])))
            source = "fred_api"
        else:
            start = (
                datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=30)
            ).strftime("%Y-%m-%d")
            response = requests.get(
                "https://fred.stlouisfed.org/graph/fredgraph.csv",
                params={"id": series_id, "cosd": start, "coed": target_date},
                headers=HEADERS,
                timeout=20,
            )
            response.raise_for_status()
            for line in response.text.splitlines()[1:]:
                obs_date, _, value = line.partition(",")
                if is_valid_number(value):
                    rows.append((obs_date.strip(), float(value)))
            source = "fred_csv"
    except Exception as exc:
        print(f"  ⚠ FRED {series_id}: {type(exc).__name__}: {exc}")
        return None
    rows = sorted((r for r in rows if r[0] <= target_date), reverse=True)
    if not rows:
        return None
    curr = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    entry = make_entry(
        curr[1] * 100, prev[1] * 100 if prev else None, curr[0], source, target_date
    )
    if entry:
        entry["unit"] = "bp"
        if entry["is_stale"]:
            entry["fallback_reason"] = "target_date_unavailable"
        print(f"  {'⚠' if entry['is_stale'] else '✅'} {series_id}: {entry['value']}bp ({entry['date']})")
    return entry

def fetch_ext(target_date):
    ext = {"indices": {}, "fx": {}, "credit": {}}
    stale, missing = [], []

    def put(group, key, name, symbol, entry):
        if entry:
            entry.update({"name": name, "symbol": symbol})
            if entry.get("is_stale"):
                stale.append(key)
        else:
            missing.append(key)
        ext[group][key] = entry

    for group, items in EXT_SYMBOLS.items():
        for key, symbol, name in items:
            print(f"\n📡 [延伸] {name} {symbol}")
            try:
                entry = ext_market_get(symbol, target_date)
            except Exception as exc:
                print(f"  ⚠ {symbol}: {type(exc).__name__}: {exc}")
                entry = None
            put(group, key, name, symbol, entry)
            time.sleep(0.3)
    for key, (series_id, name) in FRED_SERIES.items():
        print(f"\n📡 [延伸] {name} FRED {series_id}")
        try:
            entry = fred_get(series_id, target_date)
        except Exception as exc:
            print(f"  ⚠ FRED {series_id}: {type(exc).__name__}: {exc}")
            entry = None
        put("credit", key, name, f"FRED:{series_id}", entry)

    ext["quality"] = {
        "target_date": target_date,
        "stale_items": stale,
        "missing_items": missing,
        "has_stale": bool(stale),
        "has_missing": bool(missing),
    }
    print(f"\n📊 延伸指標：非同日 {len(stale)} 項、缺漏 {len(missing)} 項（不影響主要資料）")
    return ext

def fetch_all(target_date):
    data = {
        "generated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "target_date": target_date,
    }
    print("\n📡 VIX")
    data["vix"] = market_get("^VIX", target_date)
    print("\n📡 MOVE")
    data["move"] = market_get("^MOVE", target_date)
    print("\n📡 Treasury 2Y / 10Y / 30Y")
    data["y2"], data["y10"], data["y30"] = treasury_get(target_date)
    data["spread"] = (
        round((data["y10"]["value"] - data["y2"]["value"]) * 100, 2)
        if data["y2"] and data["y10"] else None
    )
    print("\n📡 SOX")
    data["sox"] = market_get("^SOX", target_date)
    data["stocks"] = {}
    for symbol, meta in STOCKS_META.items():
        print(f"\n📡 {symbol}")
        item = market_get(symbol, target_date)
        if item:
            item.update(meta)
        data["stocks"][symbol] = item
        time.sleep(0.5)
    return data

def checks_for(data):
    checks = {
        "VIX": data.get("vix"),
        "MOVE": data.get("move"),
        "SOX": data.get("sox"),
        "2Y": data.get("y2"),
        "10Y": data.get("y10"),
        "30Y": data.get("y30"),
    }
    checks.update(data.get("stocks", {}))
    return checks

def validate_data(data, target_date):
    failed = []
    stale = []
    print("\n======================================")
    print("🔎 資料完整性驗證")
    print("======================================")
    for name, item in checks_for(data).items():
        if not isinstance(item, dict):
            failed.append(name)
            print(f"❌ {name}: 無資料")
            continue
        actual_date = item.get("date")
        value = item.get("value")
        source = item.get("source", "-")
        if not actual_date or actual_date > target_date:
            failed.append(name)
            print(f"❌ {name}: invalid/future date={actual_date} [{source}]")
            continue
        if not is_valid_number(value):
            failed.append(name)
            print(f"❌ {name}: invalid value={value} [{source}]")
            continue
        item["target_date"] = target_date
        item["is_stale"] = actual_date != target_date
        if item["is_stale"]:
            stale.append(name)
            print(
                f"⚠ {name}: {actual_date} (TARGET {target_date}), "
                f"value={value} [{source}]"
            )
        else:
            print(f"✅ {name}: {actual_date}, value={value} [{source}]")
    data["data_quality"] = {
        "target_date": target_date,
        "stale_items": stale,
        "missing_items": failed,
        "has_stale": bool(stale),
        "has_missing": bool(failed),
        "all_same_day": not stale and not failed,
    }
    if failed:
        print("\n⚠️ 以下項目連最近有效資料也抓不到，本次不寫 JSON：")
        for name in failed:
            print(f"  - {name}")
        return False
    print(f"\n✅ 資料可寫入；非同日資料 {len(stale)} 項")
    return True

def load_existing_json(path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"⚠️ 無法讀取既有 {path.name}: {type(exc).__name__}: {exc}")
    return None

def preserve_manual_fields(new_data, existing_data):
    """目前先保護 CDS；第二階段人工維護頁可沿用同一優先權概念。"""
    if not isinstance(existing_data, dict):
        return
    for key in ("cds", "cds_updated_at"):
        if key in existing_data:
            new_data[key] = existing_data[key]
            print(f"🛡️ 保留人工欄位: {key}")

def write_data(data, target_date):
    market_path = DATA_DIR / f"market_{target_date}.json"
    latest_path = DATA_DIR / "latest.json"

    # 同一報表日若已有人手動補 CDS，重跑時必須保留。
    existing_market = load_existing_json(market_path)
    preserve_manual_fields(data, existing_market)

    # 若 market 檔沒有 CDS，但 latest 是同一 TARGET 且有人工 CDS，也保留。
    existing_latest = load_existing_json(latest_path)
    if (
        isinstance(existing_latest, dict)
        and existing_latest.get("target_date") == target_date
        and "cds" not in data
    ):
        preserve_manual_fields(data, existing_latest)

    market_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(f"\n✅ 已寫入 {market_path}")

    existing_latest_date = (
        existing_latest.get("target_date")
        if isinstance(existing_latest, dict)
        else None
    )
    should_update_latest = True
    if existing_latest_date:
        old_date = datetime.strptime(existing_latest_date, "%Y-%m-%d").date()
        new_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        if new_date < old_date:
            should_update_latest = False

    if should_update_latest:
        latest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        print(f"✅ 已更新 {latest_path}")
    else:
        print(
            f"ℹ️ TARGET={target_date} < latest={existing_latest_date}，"
            "不更新 latest.json"
        )

def print_summary(data):
    print("\n📊 數據摘要")
    for name, item in checks_for(data).items():
        if not item:
            continue
        marker = " ⚠ stale" if item.get("is_stale") else ""
        print(
            f"  {name}: {item['value']} | data_date={item['date']} "
            f"| source={item.get('source','-')}{marker}"
        )
    quality = data.get("data_quality", {})
    print(f"  Stale items: {quality.get('stale_items', [])}")

def refresh_ext_only(target_date):
    """只重抓延伸指標：主要資料與人工 CDS 完全不動，只替換 markets_ext。"""
    ext = fetch_ext(target_date)
    for path in (DATA_DIR / f"market_{target_date}.json", DATA_DIR / "latest.json"):
        existing = load_existing_json(path)
        if not isinstance(existing, dict) or existing.get("target_date") != target_date:
            print(f"ℹ️ {path} 不是 {target_date} 的資料，略過")
            continue
        existing["markets_ext"] = ext
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        print(f"✅ 已更新 {path} 的延伸指標")

if __name__ == "__main__":
    print("🕒 Taiwan time:", datetime.now(ZoneInfo("Asia/Taipei")).isoformat())
    print("🗓 Target market date:", TARGET)
    if "--ext-only" in sys.argv[2:]:
        refresh_ext_only(TARGET)
        sys.exit(0)
    data = fetch_all(TARGET)
    if not validate_data(data, TARGET):
        print("\n❌ DATA_UNAVAILABLE")
        sys.exit(2)
    try:
        data["markets_ext"] = fetch_ext(TARGET)
    except Exception as exc:
        print(f"⚠️ 延伸指標抓取失敗，不影響主要資料：{type(exc).__name__}: {exc}")
    write_data(data, TARGET)
    print_summary(data)
