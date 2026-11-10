"""以真實資料跑 VXN 分組 → NDX 10 日 5% 跌幅（只輸出彙總統計 JSON，不輸出原始序列）。"""
import json, sys
import numpy as np, pandas as pd
import engine as E
from phase0_inventory import fetch_yahoo

ndx = fetch_yahoo("^NDX"); vxn = fetch_yahoo("^VXN")
d = pd.concat({"ndx": ndx, "vxn": vxn}, axis=1).dropna()
H = 10
out = E.equity_drop_outcome(d.ndx, H, 0.05)
bins = [("<20", 0, 20), ("20-25", 20, 25), ("25-30", 25, 30), (">=30", 30, np.inf)]
res = {"as_of": str(d.index[-1].date()), "first": str(d.index[0].date()),
       "n_days": int(len(d)), "horizon": H, "outcome": "NDX 10日內任一收盤較觀察日跌幅>=5%",
       "current_vxn": float(d.vxn.iloc[-1]), "groups": []}
for name, lo, hi in bins:
    c = E.level_condition(d.vxn, lo, hi)
    s = E.summarize(c, out, cooldown=10, block_lengths=(20, 40, 60), n_boot=500, seed=1)
    s["group"] = name
    s["episode_stability"] = E.episode_stability(c)
    s["is_current"] = bool(c.iloc[-1])
    res["groups"].append(s)
def clean(o):
    if isinstance(o, dict): return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)): return None if np.isnan(o) else float(o)
    if isinstance(o, (np.integer,)): return int(o)
    if isinstance(o, (np.bool_,)): return bool(o)
    return o
json.dump(clean(res), open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
print("STUDY_JSON_BEGIN"); print(open(sys.argv[1]).read()); print("STUDY_JSON_END")
