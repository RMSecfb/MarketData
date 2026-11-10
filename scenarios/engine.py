"""歷史情境統計引擎（對應 strategy_discussion v3 §5）。

只做「歷史條件 vs 無條件」比較，不做預測／警示。
輸入：日頻 DataFrame（index=交易日，欄位 ndx, vxn, y10；y10 單位為 %）。
所有條件僅使用 t 日（含）以前的資料；結果使用 t+1..t+h，最後 h 天右設限（NaN）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---------- 條件 ----------

def level_condition(s: pd.Series, lo: float, hi: float = np.inf) -> pd.Series:
    """絕對水準分組 lo <= x < hi。只用當日值，無前視。"""
    return (s >= lo) & (s < hi)


def zscore_change(y: pd.Series, k: int = 20, win: int = 250) -> pd.Series:
    """k 日變動 ÷ 過去 win 個交易日「k 日變動」的標準差（只用 t 以前資料，shift(1)）。"""
    d = y.diff(k)
    sd = d.rolling(win, min_periods=win).std().shift(1)
    return d / sd


# ---------- 結果 ----------

def _forward_window(x: np.ndarray, h: int) -> np.ndarray:
    """回傳 shape (n, h)：第 i 列為 x[i+1..i+h]；不足 h 天者整列 NaN（右設限）。"""
    n = len(x)
    out = np.full((n, h), np.nan)
    for j in range(1, h + 1):
        out[: n - j, j - 1] = x[j:]
    out[n - h:, :] = np.nan  # 最後 h 天資料不足
    return out


def equity_drop_outcome(close: pd.Series, h: int = 10, thr: float = 0.05) -> pd.Series:
    """h 日內任一收盤價 <= 觀察日收盤 × (1-thr)。右設限 → NaN。"""
    w = _forward_window(close.to_numpy(float), h)
    with np.errstate(invalid="ignore"):
        hit = np.nanmin(w, axis=1) <= close.to_numpy(float) * (1 - thr)
    hit = hit.astype(float)
    hit[np.isnan(w).any(axis=1)] = np.nan
    return pd.Series(hit, index=close.index, name=f"eq_drop_{h}d")


def rate_move_outcome(y: pd.Series, h: int = 10, y_bp: float = 25.0) -> pd.Series:
    """h 日內相對起點的最大絕對偏離 >= y_bp（bp）。y 單位 %。"""
    w = _forward_window(y.to_numpy(float), h)
    dev = np.abs(w - y.to_numpy(float)[:, None]) * 100.0
    hit = (np.nanmax(dev, axis=1) >= y_bp).astype(float)
    hit[np.isnan(w).any(axis=1)] = np.nan
    return pd.Series(hit, index=y.index, name=f"rate_move_{h}d")


def joint_outcome(close: pd.Series, y: pd.Series, h: int = 10,
                  eq_thr: float = 0.05, y_bp: float = 25.0) -> pd.Series:
    """股債雙跌：第 h 日股價 <= -eq_thr 且 10Y 殖利率 >= 起點 + y_bp。"""
    c = close.to_numpy(float); yy = y.to_numpy(float)
    eq = np.full(len(c), np.nan); ry = np.full(len(c), np.nan)
    eq[:-h] = c[h:] / c[:-h] - 1
    ry[:-h] = (yy[h:] - yy[:-h]) * 100
    ok = ~np.isnan(eq) & ~np.isnan(ry)
    hit = np.where(ok, ((eq <= -eq_thr) & (ry >= y_bp)).astype(float), np.nan)
    return pd.Series(hit, index=close.index, name=f"joint_{h}d")


# ---------- 壓力時段／獨立事件 ----------

def stress_episodes(cond: pd.Series, gap: int = 60) -> list[tuple[int, int]]:
    """相鄰符合日合併：兩個符合日相隔 <= gap 個交易日視為同一時段。回傳 (起,迄) 位置。"""
    pos = np.flatnonzero(cond.to_numpy(bool))
    if len(pos) == 0:
        return []
    eps, s, prev = [], pos[0], pos[0]
    for p in pos[1:]:
        if p - prev > gap:
            eps.append((int(s), int(prev))); s = p
        prev = p
    eps.append((int(s), int(prev)))
    return eps


def cooldown_events(cond: pd.Series, cooldown: int) -> int:
    """冷卻期合併後的觸發次數：觸發後 cooldown 個交易日內的符合日不重複計。"""
    pos = np.flatnonzero(cond.to_numpy(bool)); n, last = 0, -10**9
    for p in pos:
        if p - last > cooldown:
            n += 1; last = p
    return n


def episode_stability(cond: pd.Series, gaps=(30, 60, 90)) -> dict:
    counts = {g: len(stress_episodes(cond, g)) for g in gaps}
    v = list(counts.values())
    unstable = (max(v) > 2 * max(min(v), 1)) if v else False
    return {"episodes_by_gap": counts, "flag_時段定義不穩": bool(unstable)}


# ---------- 統計與 bootstrap ----------

def _stat(c: np.ndarray, o: np.ndarray):
    n_c = c.sum()
    if n_c == 0:
        return np.nan, np.nan, np.nan, 0
    pc = o[c].mean(); pu = o.mean()
    return pc, pu, pc - pu, int(n_c)


def stationary_bootstrap_idx(n: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """Politis–Romano 環狀 stationary bootstrap 的索引（向量化）。"""
    jump = rng.random(n) < 1.0 / mean_block
    jump[0] = True
    bid = np.cumsum(jump) - 1
    first = np.flatnonzero(jump)
    starts = rng.integers(n, size=len(first))
    return (starts[bid] + (np.arange(n) - first[bid])) % n


def conditional_vs_baseline(cond: pd.Series, outcome: pd.Series,
                            block_lengths=(20, 40, 60), n_boot: int = 2000,
                            alpha: float = 0.05, seed: int = 0) -> dict:
    """配對列 (cond_t, outcome_t) 在完整日期序列上整列重抽。
    條件比例、基準、差異在「同一個重抽樣本」內重算；右設限列（outcome NaN）先剔除。"""
    df = pd.DataFrame({"c": cond.astype(bool), "o": outcome}).dropna(subset=["o"])
    c = df["c"].to_numpy(bool); o = df["o"].to_numpy(float); n = len(df)
    pc, pu, diff, n_c = _stat(c, o)
    res = {"n_eligible": n, "observation_days": n_c,
           "events_cond": int(o[c].sum()), "events_base": int(o.sum()),
           "p_cond": pc, "p_base": pu, "diff": diff, "ci": {}}
    rng = np.random.default_rng(seed)
    for L in block_lengths:
        d, usable = [], 0
        for _ in range(n_boot):
            ix = stationary_bootstrap_idx(n, L, rng)
            _, _, dd, k = _stat(c[ix], o[ix])
            if k > 0:
                d.append(dd); usable += 1
        d = np.asarray(d)
        res["ci"][L] = {
            "lo": float(np.quantile(d, alpha / 2)) if usable else np.nan,
            "hi": float(np.quantile(d, 1 - alpha / 2)) if usable else np.nan,
            "usable_frac": usable / n_boot,
        }
    return res


def halves_agree(cond: pd.Series, outcome: pd.Series) -> bool:
    """前後半段條件差異方向一致。"""
    df = pd.DataFrame({"c": cond.astype(bool), "o": outcome}).dropna(subset=["o"])
    m = len(df) // 2; ds = []
    for part in (df.iloc[:m], df.iloc[m:]):
        _, _, d, k = _stat(part["c"].to_numpy(bool), part["o"].to_numpy(float))
        ds.append(d if k else np.nan)
    return bool(np.all(~np.isnan(ds)) and np.sign(ds[0]) == np.sign(ds[1]) != 0)


def summarize(cond: pd.Series, outcome: pd.Series, cooldown: int,
              min_obs: int = 30, min_episodes: int = 5, max_year_share: float = 0.5,
              **boot_kw) -> dict:
    """四態摘要（只對預先指定的 10 日主要結果使用）。"""
    st = conditional_vs_baseline(cond, outcome, **boot_kw)
    eps = stress_episodes(cond)
    ev = cooldown_events(cond, cooldown)
    yrs = cond[cond].index.year
    top = float(pd.Series(yrs).value_counts(normalize=True).max()) if len(yrs) else np.nan
    flags = []
    if top > max_year_share: flags.append("單一年度主導")
    if len(eps) < min_episodes: flags.append("少數壓力時段主導")
    flags += (["時段定義不穩"] if episode_stability(cond)["flag_時段定義不穩"] else [])
    if st["n_eligible"] == 0:
        state = "資料不可用"
    elif st["observation_days"] < min_obs or len(eps) < min_episodes:
        state = "樣本不足"
    else:
        cis = list(st["ci"].values())
        excl = all(ci["lo"] > 0 or ci["hi"] < 0 for ci in cis if not np.isnan(ci["lo"]))
        agree = halves_agree(cond, outcome)
        if excl and agree and "單一年度主導" not in flags:
            state = "歷史條件事件比例較高" if st["diff"] > 0 else "歷史條件事件比例較低"
        else:
            state = "差異不明確"
    return {**st, "independent_events": ev, "stress_episodes": len(eps),
            "top_year_share": top, "quality_flags": flags, "state": state}
