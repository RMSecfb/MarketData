import numpy as np, pandas as pd
import engine as E


def synth(n=3000, seed=1, boost=0.0):
    """VXN 為 AR(1) 水準；NDX 報酬波動隨 VXN 上升（boost>0 時條件才有真效果）。"""
    r = np.random.default_rng(seed)
    v = np.empty(n); v[0] = 20
    for i in range(1, n):
        v[i] = 20 + 0.97 * (v[i - 1] - 20) + r.normal(0, 1.2)
    v = np.clip(v, 9, 80)
    vol = (0.008 + boost * (v - 20).clip(0) / 1000)
    ndx = 1000 * np.exp(np.cumsum(r.normal(0, vol)))
    y = 3 + np.cumsum(r.normal(0, 0.05, n))
    idx = pd.bdate_range("2005-01-03", periods=n)
    return pd.DataFrame({"ndx": ndx, "vxn": v, "y10": y}, index=idx)


def test_right_censoring():
    d = synth(200)
    o = E.equity_drop_outcome(d.ndx, 10)
    assert o.iloc[-10:].isna().all() and o.iloc[:-10].notna().all()
    o2 = E.rate_move_outcome(d.y10, 10)
    assert o2.iloc[-10:].isna().all()


def test_outcome_values():
    c = pd.Series([100, 99, 94, 100, 100, 100], index=pd.bdate_range("2020-01-01", periods=6))
    o = E.equity_drop_outcome(c, h=3, thr=0.05)
    assert o.iloc[0] == 1.0      # 94 <= 95
    assert o.iloc[1] == 1.0      # 99*0.95=94.05，94 <= 94.05
    assert o.iloc[3] == 0.0 or np.isnan(o.iloc[3])
    assert np.isnan(o.iloc[3:]).all()


def test_no_lookahead_condition():
    d = synth(1500)
    z1 = E.zscore_change(d.y10)
    d2 = d.copy(); d2.iloc[1000:, d2.columns.get_loc("y10")] += 5  # 改動未來
    z2 = E.zscore_change(d2.y10)
    assert np.allclose(z1.iloc[:1000].dropna(), z2.iloc[:1000].dropna())


def test_episodes_and_cooldown():
    idx = pd.bdate_range("2020-01-01", periods=400)
    c = pd.Series(False, index=idx)
    c.iloc[[10, 11, 12, 50, 200, 201]] = True
    assert E.stress_episodes(c, 60) == [(10, 50), (200, 201)]
    assert len(E.stress_episodes(c, 30)) == 3
    assert E.cooldown_events(c, 10) == 3   # 10~12、50、200~201 各合併為一次


def test_null_ci_coverage_and_power():
    # 無效果（boost=0）：差異區間應多半涵蓋 0；有效果（boost 大）：應排除 0
    cover = 0
    for sd in range(8):
        d = synth(2500, seed=10 + sd, boost=0.0)
        cond = E.level_condition(d.vxn, 25)
        o = E.equity_drop_outcome(d.ndx, 10)
        r = E.conditional_vs_baseline(cond, o, block_lengths=(40,), n_boot=300, seed=sd)
        ci = r["ci"][40]
        cover += (ci["lo"] <= 0 <= ci["hi"]) if r["observation_days"] > 0 else 1
    assert cover >= 6, cover
    d = synth(3000, seed=99, boost=6.0)
    cond = E.level_condition(d.vxn, 25)
    o = E.equity_drop_outcome(d.ndx, 10)
    s = E.summarize(cond, o, cooldown=10, block_lengths=(40,), n_boot=300)
    assert s["diff"] > 0, s


def test_summarize_states():
    d = synth(300, seed=3)
    cond = E.level_condition(d.vxn, 200)  # 永不符合
    o = E.equity_drop_outcome(d.ndx, 10)
    s = E.summarize(cond, o, cooldown=10, block_lengths=(20,), n_boot=50)
    assert s["state"] == "樣本不足"
