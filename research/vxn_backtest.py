"""VXN 科技股風險預警：歷史資料檢查與回測（離線研究，不影響每日排程與線上資料）。

用法：
    python research/vxn_backtest.py            # 從 Yahoo 抓歷史並產出報告
    python research/vxn_backtest.py --csv DIR  # 改讀 DIR 內的 VXN.csv、VIX.csv、NDX.csv、SOX.csv（date,close）

輸出（research/output/）：
    data_check.json   各序列筆數、起訖日、缺值
    scenarios.csv     訊號 × 事件定義 × 期間 的完整情境表
    report.md         摘要報告

方法在執行前固定，不依結果調整：
  - 訊號門檻採用規劃草案第 5 節的 PoC 數值，不做最佳化。
  - 事件：訊號日收盤後 H 個交易日內，NDX 收盤相對訊號日收盤的最大跌幅 ≥ X%。
    X ∈ {3, 5, 8}，H ∈ {5, 10, 20}，全部列出。
  - 百分位只用當日以前的資料（過去 252 個有效日，至少 126 日）。
  - 連續訊號合併為一次（冷卻 10 個交易日），避免重複計數。
  - 期間分為前段（2018 年底以前）與後段（2019 年起），檢查結果是否穩定。
原始歷史數值不寫入 repo，只輸出統計結果。
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

OUT = Path(__file__).parent / "output"
SYMBOLS = {"VXN": "^VXN", "VIX": "^VIX", "NDX": "^NDX", "SOX": "^SOX"}
HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}
START = "2001-02-01"          # VXN 約自 2001 年起有資料
SPLIT = "2019-01-01"          # 前段／後段分界
PCT_WINDOW, PCT_MIN = 252, 126
COOLDOWN = 10
DROPS = (3, 5, 8)
HORIZONS = (5, 10, 20)


# ── 資料 ────────────────────────────────────────────────
def yahoo_history(symbol):
    import requests
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    params = {"interval": "1d", "period1": 978307200, "period2": int(time.time()),
              "includePrePost": "false"}
    for host in HOSTS:
        for attempt in range(3):
            try:
                r = requests.get(f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}",
                                 params=params, headers=HEADERS, timeout=30)
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "America/New_York")
                closes = res["indicators"]["quote"][0]["close"]
                rows = {}
                for ts, c in zip(res["timestamp"], closes):
                    if c is None:
                        continue
                    d = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).date()
                    rows[pd.Timestamp(d)] = float(c)
                s = pd.Series(rows).sort_index()
                if len(s) > 1000:
                    return s, f"yahoo_chart:{host}"
                raise ValueError(f"only {len(s)} rows")
            except Exception as exc:
                print(f"  ⚠ {symbol} {host} #{attempt + 1}: {type(exc).__name__}: {exc}")
                time.sleep(3)
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(start=START, auto_adjust=False)
        s = h["Close"].dropna()
        s.index = pd.to_datetime(s.index.date)
        if len(s) > 1000:
            return s.astype(float), "yfinance"
    except Exception as exc:
        print(f"  ⚠ {symbol} yfinance: {type(exc).__name__}: {exc}")
    return None, None


def load_data(csv_dir=None):
    series, sources = {}, {}
    for name, symbol in SYMBOLS.items():
        if csv_dir:
            df = pd.read_csv(Path(csv_dir) / f"{name}.csv", parse_dates=["date"])
            s = pd.to_numeric(df.set_index("date")["close"], errors="coerce").dropna().sort_index()
            src = "csv"
        else:
            print(f"📡 {symbol}")
            s, src = yahoo_history(symbol)
            time.sleep(1)
        if s is None:
            print(f"❌ {symbol} 抓不到歷史資料")
            continue
        series[name] = s[s.index >= START]
        sources[name] = src
    return series, sources


def data_check(series, sources):
    check = {}
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
    except Exception:
        nyse = None
    for name, s in series.items():
        item = {
            "symbol": SYMBOLS[name], "source": sources[name], "rows": int(len(s)),
            "first": s.index[0].strftime("%Y-%m-%d"), "last": s.index[-1].strftime("%Y-%m-%d"),
            "min": round(float(s.min()), 2), "max": round(float(s.max()), 2),
            "non_positive": int((s <= 0).sum()),
            "max_abs_daily_change_pct": round(float(s.pct_change().abs().max() * 100), 1),
        }
        if nyse is not None:
            days = nyse.schedule(start_date=s.index[0], end_date=s.index[-1]).index.normalize()
            missing = days.difference(s.index)
            item["nyse_days"] = int(len(days))
            item["missing_days"] = int(len(missing))
            item["missing_recent"] = [d.strftime("%Y-%m-%d") for d in missing[-10:]]
            item["missing_by_year"] = {str(k): int(v) for k, v in
                                       pd.Series(1, index=missing).groupby(missing.year).sum().items()}
        check[name] = item
    return check


# ── 訊號 ────────────────────────────────────────────────
def trailing_pct(s):
    """當日數值在過去 252 個有效日（不含當日）中的百分位。"""
    def rank(w):
        hist, cur = w[:-1], w[-1]
        hist = hist[~np.isnan(hist)]
        if len(hist) < PCT_MIN or np.isnan(cur):
            return np.nan
        return float((hist <= cur).mean() * 100)
    return s.rolling(PCT_WINDOW + 1, min_periods=PCT_MIN + 1).apply(rank, raw=True)


def spike(s):
    d1, r1, r5 = s.diff(), s.pct_change(), s.pct_change(5)
    return ((d1 >= 3) & (r1 >= 0.15)) | (r5 >= 0.25)


def build(series):
    df = pd.DataFrame({k: series[k] for k in ("VXN", "VIX", "NDX")}).dropna()
    if "SOX" in series:
        df["SOX"] = series["SOX"].reindex(df.index)
    df["ratio"] = df["VXN"] / df["VIX"]
    df["vxn_pct"] = trailing_pct(df["VXN"])
    df["vix_pct"] = trailing_pct(df["VIX"])
    df["ratio_pct"] = trailing_pct(df["ratio"])
    df["ndx_ret5"] = df["NDX"].pct_change(5) * 100
    df["ndx_ret1"] = df["NDX"].pct_change() * 100
    df["ndx_dd5"] = (df["NDX"] / df["NDX"].rolling(5).max() - 1) * 100

    A = spike(df["VXN"])
    B = df["ratio_pct"] >= 90
    C = df["vxn_pct"] >= 80
    sig = {
        "A 快速升溫（VXN）": A,
        "B 比率異常（VXN/VIX ≥90 百分位）": B,
        "C 自身高檔（VXN ≥80 百分位）": C,
        "觀察＝A 且無 B、C": A & ~B & ~C,
        "L1＝A 且（B 或 C）": A & (B | C),
        "D＝A 且 NDX 近 5 日回撤 <3%（價格尚未確認）": A & (df["ndx_dd5"] > -3),
        "基準｜VIX 快速升溫（同 A 規則）": spike(df["VIX"]),
        "基準｜VIX ≥80 百分位": df["vix_pct"] >= 80,
        "基準｜NDX 單日跌 ≥2%": df["ndx_ret1"] <= -2,
    }
    valid = df["vxn_pct"].notna() & df["ratio_pct"].notna() & df["vix_pct"].notna()
    return df[valid], {k: v[valid].fillna(False) for k, v in sig.items()}


def forward_stats(ndx, horizon):
    """未來 horizon 日的最大跌幅（%，負值）與首次達到各跌幅的天數。"""
    v = ndx.values
    n = len(v)
    mdd = np.full(n, np.nan)
    first = {x: np.full(n, np.nan) for x in DROPS}
    for i in range(n - horizon):
        path = v[i + 1:i + 1 + horizon] / v[i] - 1
        mdd[i] = path.min() * 100
        for x in DROPS:
            hit = np.nonzero(path <= -x / 100)[0]
            if len(hit):
                first[x][i] = hit[0] + 1
    return mdd, first


def episodes(flag):
    """連續訊號合併：距上一次訊號超過 COOLDOWN 個交易日才算新的一次。"""
    idx = np.nonzero(flag.values)[0]
    keep, last = [], -10**9
    for i in idx:
        if i - last > COOLDOWN:
            keep.append(i)
        last = i
    return np.array(keep, dtype=int)


def evaluate(df, signals):
    rows = []
    fwd = {h: forward_stats(df["NDX"], h) for h in HORIZONS}
    periods = {
        "全期間": np.ones(len(df), bool),
        f"前段（{SPLIT[:4]} 以前）": (df.index < SPLIT),
        f"後段（{SPLIT[:4]} 起）": (df.index >= SPLIT),
    }
    years = max((df.index[-1] - df.index[0]).days / 365.25, 1e-9)
    for name, flag in signals.items():
        ep_all = episodes(flag)
        for pname, pmask in periods.items():
            ep = ep_all[pmask[ep_all]] if len(ep_all) else ep_all
            p_years = max(pmask.sum() / 252, 1e-9)
            for h in HORIZONS:
                mdd, first = fwd[h]
                ok = ~np.isnan(mdd)
                ep_h = ep[ok[ep]] if len(ep) else ep
                for x in DROPS:
                    event = mdd <= -x
                    base = float(event[ok & pmask].mean()) if (ok & pmask).any() else np.nan
                    n = len(ep_h)
                    hits = int(event[ep_h].sum()) if n else 0
                    prec = hits / n if n else np.nan
                    lead = first[x][ep_h][event[ep_h]] if n else np.array([])
                    # 事件日涵蓋率：事件成立的日子中，當日或前 4 日內曾出現訊號的比例
                    recent = flag.rolling(5, min_periods=1).max().values.astype(bool)
                    ev_days = event & ok & pmask
                    recall = float(recent[ev_days].mean()) if ev_days.any() else np.nan
                    rows.append({
                        "訊號": name, "期間": pname, "未來交易日": h, "NDX 最大跌幅門檻%": x,
                        "訊號次數": n, "每年次數": round(n / p_years, 2),
                        "命中次數": hits,
                        "命中率%": round(prec * 100, 1) if n else None,
                        "基準率%": round(base * 100, 1) if base == base else None,
                        "倍數": round(prec / base, 2) if n and base and base > 0 else None,
                        "誤報率%": round((1 - prec) * 100, 1) if n else None,
                        "事件日涵蓋率%": round(recall * 100, 1) if recall == recall else None,
                        "命中時中位領先日": float(np.median(lead)) if len(lead) else None,
                        "命中時隔日即達標比例%": round(float((lead <= 1).mean() * 100), 1) if len(lead) else None,
                    })
    return pd.DataFrame(rows), years


def coincidence(df, signals):
    """訊號當下價格是否已經下跌：判斷同步或領先。"""
    rows = []
    for name, flag in signals.items():
        ep = episodes(flag)
        if not len(ep):
            continue
        r5, r1 = df["ndx_ret5"].values[ep], df["ndx_ret1"].values[ep]
        rows.append({
            "訊號": name, "次數": len(ep),
            "訊號日 NDX 當日報酬中位數%": round(float(np.nanmedian(r1)), 2),
            "訊號日 NDX 近 5 日報酬中位數%": round(float(np.nanmedian(r5)), 2),
            "訊號時 NDX 近 5 日已跌 ≥3% 比例%": round(float((r5 <= -3).mean() * 100), 1),
            "訊號日 NDX 當日下跌比例%": round(float((r1 < 0).mean() * 100), 1),
        })
    return pd.DataFrame(rows)


def lead_lag(df):
    """VXN 日變動率與 NDX 日報酬在不同時差下的相關係數。
    lag>0：VXN 變動在前、NDX 報酬在後（VXN 是否領先）。"""
    dv, dn = df["VXN"].pct_change(), df["NDX"].pct_change()
    return pd.DataFrame([{"時差（日）": k, "相關係數": round(float(dv.corr(dn.shift(-k))), 3)}
                         for k in range(-5, 6)])


# ── 波動預測力（VXN 本身的意義：預期波動幅度，不是方向）────────
def vol_tests(df):
    """① VXN 與未來 20 日實現波動率 ② VXN 與未來 5 日絕對報酬 ③ 依 VXN 高低分組的未來最大回撤。"""
    d = df.copy()
    lr = np.log(d["NDX"]).diff()
    ann = np.sqrt(252) * 100
    d["rv20_past"] = lr.rolling(20).std() * ann
    d["rv20_fut"] = d["rv20_past"].shift(-20)            # t+1..t+20 的實現波動率（年化 %）
    d["abs5_fut"] = (d["NDX"].shift(-5) / d["NDX"] - 1).abs() * 100
    d["ret20_fut"] = (d["NDX"].shift(-20) / d["NDX"] - 1) * 100
    for h in HORIZONS:
        mdd, _ = forward_stats(d["NDX"], h)
        d[f"mdd{h}"] = mdd
    d["implied5"] = d["VXN"] * np.sqrt(5 / 252) * np.sqrt(2 / np.pi)   # VXN 隱含的 5 日平均絕對變動 %

    periods = {"全期間": d.index >= d.index[0], f"前段（{SPLIT[:4]} 以前）": d.index < SPLIT,
               f"後段（{SPLIT[:4]} 起）": d.index >= SPLIT}
    corr_rows = []
    tests = [("① 未來 20 日實現波動率", "rv20_fut", 20), ("② 未來 5 日絕對報酬", "abs5_fut", 5)]
    for pname, pm in periods.items():
        x = d[pm]
        for label, target, step in tests:
            for pred, pcol in (("VXN", "VXN"), ("VIX", "VIX"), ("過去 20 日實現波動率", "rv20_past")):
                y = x[[pcol, target]].dropna()
                nov = y.iloc[::step]                      # 不重疊樣本
                corr_rows.append({
                    "檢驗": label, "期間": pname, "預測變數": pred, "樣本數": len(y),
                    "Pearson": round(float(y[pcol].corr(y[target])), 3),
                    "Spearman": round(float(y[pcol].rank().corr(y[target].rank())), 3),
                    "不重疊樣本 Pearson": round(float(nov[pcol].corr(nov[target])), 3),
                    "不重疊樣本數": len(nov),
                })
    corr = pd.DataFrame(corr_rows)

    def group_table(key, order):
        g = d.dropna(subset=["rv20_fut"]).groupby(key, observed=True)
        t = pd.DataFrame({
            "天數": g.size(),
            "VXN 平均": g["VXN"].mean(),
            "未來 20 日實現波動率%": g["rv20_fut"].mean(),
            "未來 5 日絕對報酬%（平均）": g["abs5_fut"].mean(),
            "VXN 隱含 5 日變動%": g["implied5"].mean(),
            "未來 5 日最大回撤%（平均）": g["mdd5"].mean(),
            "未來 10 日最大回撤%（平均）": g["mdd10"].mean(),
            "未來 20 日最大回撤%（平均）": g["mdd20"].mean(),
            "未來 20 日最大回撤%（最差 5%）": g["mdd20"].quantile(0.05),
            "10 日內回撤 ≥5% 機率%": g["mdd10"].apply(lambda v: (v <= -5).mean() * 100),
            "20 日內回撤 ≥8% 機率%": g["mdd20"].apply(lambda v: (v <= -8).mean() * 100),
            "未來 20 日報酬%（平均）": g["ret20_fut"].mean(),
        }).reindex(order).round(2)
        t.index.name = "分組"
        return t.reset_index()

    pct_labels = ["0–20", "20–40", "40–60", "60–80", "80–90", "90–100"]
    d["pct_group"] = pd.cut(d["vxn_pct"], [-0.1, 20, 40, 60, 80, 90, 100], labels=pct_labels)
    lvl_labels = ["<15", "15–20", "20–25", "25–30", "30–40", "≥40"]
    d["lvl_group"] = pd.cut(d["VXN"], [0, 15, 20, 25, 30, 40, 1000], labels=lvl_labels, right=False)
    by_pct = group_table("pct_group", pct_labels)
    by_lvl = group_table("lvl_group", lvl_labels)
    by_lvl_late = None
    late = d[d.index >= SPLIT]
    if len(late):
        keep = d
        d = late
        by_lvl_late = group_table("lvl_group", lvl_labels)
        d = keep
    return corr, by_pct, by_lvl, by_lvl_late


def md_table(d):
    d = d.astype(object).where(d.notna(), "—")
    head = "| " + " | ".join(map(str, d.columns)) + " |\n|" + "---|" * len(d.columns) + "\n"
    return head + "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in d.values) + "\n"


def main():
    csv_dir = sys.argv[sys.argv.index("--csv") + 1] if "--csv" in sys.argv else None
    OUT.mkdir(parents=True, exist_ok=True)
    series, sources = load_data(csv_dir)
    check = data_check(series, sources)
    (OUT / "data_check.json").write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(check, ensure_ascii=False, indent=2))
    if any(k not in series for k in ("VXN", "VIX", "NDX")):
        (OUT / "report.md").write_text("# VXN 回測\n\n資料不足：VXN、VIX、NDX 至少一項抓不到，未執行回測。\n",
                                       encoding="utf-8")
        print("❌ 必要序列不足，只輸出資料檢查")
        return 2

    df, signals = build(series)
    table, years = evaluate(df, signals)
    table.to_csv(OUT / "scenarios.csv", index=False, encoding="utf-8-sig")
    coin, ll = coincidence(df, signals), lead_lag(df)
    coin.to_csv(OUT / "coincidence.csv", index=False, encoding="utf-8-sig")
    ll.to_csv(OUT / "lead_lag.csv", index=False, encoding="utf-8-sig")
    yearly = pd.DataFrame({k: pd.Series(1, index=df.index[episodes(v)]).groupby(lambda d: d.year).sum()
                           for k, v in signals.items()}).fillna(0).astype(int)
    yearly.index.name = "年度"
    yearly.to_csv(OUT / "signals_by_year.csv", encoding="utf-8-sig")

    focus = table[(table["未來交易日"] == 10) & (table["NDX 最大跌幅門檻%"] == 5)]
    cols = ["訊號", "訊號次數", "每年次數", "命中率%", "基準率%", "倍數", "事件日涵蓋率%",
            "命中時中位領先日", "命中時隔日即達標比例%"]
    parts = [
        "# VXN 科技股風險預警｜歷史回測結果\n",
        f"- 樣本：{df.index[0]:%Y-%m-%d} ～ {df.index[-1]:%Y-%m-%d}，{len(df)} 個交易日（約 {years:.1f} 年）",
        f"- 資料來源：{', '.join(f'{k}={v}' for k, v in sources.items())}",
        "- 門檻為規劃草案的 PoC 數值，未依結果調整；完整情境見 `scenarios.csv`。",
        "- 命中率：訊號後 H 日內 NDX 最大跌幅達門檻的比例；基準率：任一天往後看同樣條件成立的比例；倍數＝命中率÷基準率。\n",
        "## 1. 資料檢查\n",
        md_table(pd.DataFrame([{"序列": k, **{c: v.get(c) for c in
                                             ("rows", "first", "last", "missing_days", "min", "max")}}
                               for k, v in check.items()])),
        "## 2. 示例情境：未來 10 日 NDX 最大跌幅 ≥5%\n",
    ]
    for p in focus["期間"].unique():
        parts += [f"### {p}\n", md_table(focus[focus["期間"] == p][cols])]
    parts += [
        "## 3. 訊號出現時，價格是否已經下跌（同步或領先）\n", md_table(coin),
        "## 4. VXN 日變動與 NDX 日報酬的時差相關\n",
        "時差 0 為同日；正值代表 VXN 變動在前、NDX 報酬在後。若正時差的相關係數接近 0，表示 VXN 的日變動對之後幾天的報酬沒有線性預測力。\n",
        md_table(ll),
        "## 5. 各年度訊號次數\n", md_table(yearly.reset_index()),
    ]
    corr, by_pct, by_lvl, by_lvl_late = vol_tests(df)
    corr.to_csv(OUT / "vol_corr.csv", index=False, encoding="utf-8-sig")
    by_pct.to_csv(OUT / "vol_by_percentile.csv", index=False, encoding="utf-8-sig")
    by_lvl.to_csv(OUT / "vol_by_level.csv", index=False, encoding="utf-8-sig")
    parts += [
        "## 6. VXN 對未來波動幅度的預測力\n",
        "實現波動率＝NDX 日對數報酬標準差年化；不重疊樣本每 20 日（①）或 5 日（②）取一筆，避免重疊窗口高估。"
        "「過去 20 日實現波動率」是不需要 VXN 的簡單對照。\n",
        md_table(corr),
        "## 7. 依 VXN 歷史百分位分組（過去 252 日，當下可得）\n", md_table(by_pct),
        "## 8. 依 VXN 絕對水準分組（全期間）\n", md_table(by_lvl),
    ]
    if by_lvl_late is not None:
        by_lvl_late.to_csv(OUT / "vol_by_level_late.csv", index=False, encoding="utf-8-sig")
        parts += [f"## 9. 依 VXN 絕對水準分組（{SPLIT[:4]} 起）\n", md_table(by_lvl_late)]
    (OUT / "report.md").write_text("\n".join(parts), encoding="utf-8")
    print(f"✅ 已輸出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
