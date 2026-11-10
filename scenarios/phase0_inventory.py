"""Phase 0 資料盤點（請在能連 Yahoo／FRED 的環境執行，例如你的 GitHub Actions）。

用法：python phase0_inventory.py --out inventory_report.json
只輸出「盤點摘要」（起訖日、筆數、缺值、極端跳動、最長斷檔），
不輸出、不提交原始第三方時間序列（授權未確認）。
"""
import argparse, json, io
import numpy as np, pandas as pd

YAHOO = {"ndx": "^NDX", "spx": "^GSPC", "vxn": "^VXN", "vix": "^VIX",
         "sox": "^SOX", "move": "^MOVE", "ixic": "^IXIC"}
FRED = {"y10": "DGS10", "y2": "DGS2", "hy_oas": "BAMLH0A0HYM2"}


def fetch_yahoo(sym):
    import yfinance as yf
    d = yf.download(sym, start="1990-01-01", auto_adjust=False, progress=False)
    s = d["Close"].squeeze().dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s


def fetch_fred(sid):
    import urllib.request
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
    txt = urllib.request.urlopen(url, timeout=60).read().decode()
    d = pd.read_csv(io.StringIO(txt), index_col=0, parse_dates=True)
    return pd.to_numeric(d.iloc[:, 0], errors="coerce").dropna()


def profile(name, s):
    gaps = s.index.to_series().diff().dt.days.dropna()
    ret = s.pct_change().abs() if name not in ("y10", "y2", "hy_oas") else s.diff().abs()
    return {"series": name, "first": str(s.index[0].date()), "last": str(s.index[-1].date()),
            "n": int(len(s)), "max_gap_days": int(gaps.max()) if len(gaps) else None,
            "gaps_gt_7d": int((gaps > 7).sum()),
            "nonpositive": int((s <= 0).sum()),
            "top5_abs_moves": [float(x) for x in ret.nlargest(5).values],
            "dup_dates": int(s.index.duplicated().sum())}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="inventory_report.json")
    a = ap.parse_args(); rep, errs = [], {}
    for k, v in YAHOO.items():
        try: rep.append(profile(k, fetch_yahoo(v)))
        except Exception as e: errs[k] = repr(e)
    for k, v in FRED.items():
        try: rep.append(profile(k, fetch_fred(v)))
        except Exception as e: errs[k] = repr(e)
    json.dump({"profiles": rep, "errors": errs,
               "todo_manual": ["確認 FRED ICE BofA(HY OAS) 再散布授權與可取得歷史長度",
                               "確認 2Y 即時來源與發布延遲",
                               "確認 MOVE 歷史起點與是否為 Yahoo 補值"]},
              open(a.out, "w"), ensure_ascii=False, indent=2)
    print(json.dumps({"ok": [r["series"] for r in rep], "errors": errs}, ensure_ascii=False))
