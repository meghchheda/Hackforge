"""Block-bootstrap scenario engine (FR-18), reserve quantile table (FR-04) and wrong-way risk check (FR-20)."""
from __future__ import annotations

import numpy as np
import pandas as pd

QS = (0.5, 0.9, 0.95, 0.975, 0.99, 0.995, 0.999)
MAX_H = 72


class ScenarioEngine:
    def __init__(self, df: pd.DataFrame, pool_end: int, seed: int = 42, block: float = 24.0):
        self.df = df
        self.seed = seed
        self.block = block
        nf = df["net_flow_usd"].values
        how = df["how"].values
        hist = slice(0, pool_end)
        self.profile = pd.Series(nf[hist]).groupby(how[hist]).mean().reindex(range(168)).values
        self.resid_all = nf - self.profile[how]
        self.pool = self.resid_all[hist]
        self.pool_hour = df["hour"].values[hist]
        eq = df["tier3_stock_index_price"].values
        self.eq_ret = np.r_[0.0, eq[1:] / eq[:-1] - 1][hist]
        # blocks restart at the same hour of the week, so weekday-specific volatility is kept
        pool_how = df["how"].values[hist]
        self.by_how = [np.where(pool_how[:-MAX_H] == h)[0] for h in range(168)]
        r = self.pool
        self.rho = float(np.corrcoef(r[1:], r[:-1])[0, 1])
        self.joint = False
        self._table()

    # ---------- path generation ----------
    def paths(self, start_how: int, length: int, n: int, e0: float = 0.0, seed: int | None = None,
              with_equity: bool = False):
        rng = np.random.default_rng(self.seed if seed is None else seed)
        p_new = 1.0 / self.block
        idx = np.empty(n, dtype=np.int64)
        out = np.empty((n, length))
        eq = np.empty((n, length)) if with_equity else None
        for k in range(length):
            how = (start_how + 1 + k) % 168
            if k == 0:
                jump = np.ones(n, bool)
            else:
                idx += 1
                jump = (rng.random(n) < p_new) | (idx >= len(self.pool))   # a block that reaches the end restarts
            cand = self.by_how[how]
            idx[jump] = cand[rng.integers(0, len(cand), jump.sum())]
            out[:, k] = self.profile[how] + self.pool[idx] + (self.rho ** (k + 1)) * e0
            if with_equity:
                eq[:, k] = self.eq_ret[idx]
        return (out, eq) if with_equity else out

    def _table(self, n: int = 2000):
        """Peak cumulative net outflow quantiles by start hour-of-week and horizon."""
        self.table = np.zeros((168, MAX_H, len(QS)))
        for how in range(168):
            p = self.paths(how, MAX_H, n, seed=self.seed + how)
            peak = np.maximum.accumulate(np.cumsum(-p, axis=1), axis=1).clip(min=0)
            self.table[how] = np.quantile(peak, QS, axis=0).T

    def peak_quantile(self, how: int, H: int, q: float) -> float:
        qi = QS.index(q) if q in QS else int(np.argmin([abs(x - q) for x in QS]))
        return float(self.table[how, min(H, MAX_H) - 1, qi])

    def momentum(self, e0: float, H: int) -> float:
        rho = self.rho
        return float(-e0 * rho * (1 - rho ** H) / (1 - rho))

    def autocorr_check(self) -> dict:
        rng = np.random.default_rng(self.seed)
        sims = []
        for i in range(30):
            s = self.paths(int(rng.integers(0, 168)), 720, 1, seed=self.seed + 1000 + i)[0]
            sims.append(np.corrcoef(s[1:], s[:-1])[0, 1])
        obs = float(self.df["net_flow_usd"].autocorr(1))
        iid = self._iid_autocorr()
        return {"observed": obs, "bootstrap": float(np.mean(sims)), "iid": iid,
                "block_hours": self.block, "seed": self.seed}

    def _iid_autocorr(self) -> float:
        rng = np.random.default_rng(self.seed)
        how = np.arange(720) % 168
        s = self.profile[how] + rng.choice(self.pool, 720)
        return float(np.corrcoef(s[1:], s[:-1])[0, 1])

    def fan(self, t: int, length: int = 48, n: int = 1000) -> dict:
        """Cumulative net-flow fan from hour t for the runway clock."""
        how = int(self.df["how"].iloc[t])
        p = self.paths(how, length, n, e0=float(self.resid_all[t]), seed=self.seed + t)
        cum = np.cumsum(p, axis=1)
        qs = np.quantile(cum, [0.01, 0.05, 0.5, 0.95, 0.99], axis=0) / 1e6
        return {"p01": qs[0].round(2).tolist(), "p05": qs[1].round(2).tolist(), "p50": qs[2].round(2).tolist(),
                "p95": qs[3].round(2).tolist(), "p99": qs[4].round(2).tolist(), "cum_paths": cum}


def wrong_way(df: pd.DataFrame, reps: int = 400, seed: int = 3) -> dict:
    """FR-20: do large outflows coincide with equity drops or yield rises?"""
    nf = df["net_flow_usd"].values
    eq = df["tier3_stock_index_price"].values
    y = df["tier2_bond_yield_pct"].values
    eq_ret = np.r_[0.0, eq[1:] / eq[:-1] - 1]
    dy = np.r_[0.0, np.diff(y)]
    lags = list(range(0, 25, 2))
    corr_eq = [float(np.corrcoef(nf[l:], eq_ret[:len(nf) - l])[0, 1]) for l in lags]
    corr_y = [float(np.corrcoef(nf[l:], dy[:len(nf) - l])[0, 1]) for l in lags]

    top = nf <= np.quantile(nf, 0.05)
    diff_obs = float(eq_ret[top].mean() - eq_ret[~top].mean())
    rng = np.random.default_rng(seed)
    n, block = len(nf), 24
    diffs, corr0 = [], []
    for _ in range(reps):
        starts = rng.integers(0, n - block, n // block)
        ix = (starts[:, None] + np.arange(block)).ravel()
        t, r = top[ix], eq_ret[ix]
        if t.sum() and (~t).sum():
            diffs.append(r[t].mean() - r[~t].mean())
        corr0.append(np.corrcoef(nf[ix], eq_ret[ix])[0, 1])
    lo, hi = np.quantile(diffs, [0.025, 0.975])
    clo, chi = np.quantile(corr0, [0.025, 0.975])
    significant = bool(lo > 0 or hi < 0) or bool(clo > 0 or chi < 0)
    return {
        "lags": lags, "corr_equity": corr_eq, "corr_yield": corr_y,
        "top5_eq_ret_diff_bp": diff_obs * 1e4, "ci_bp": [lo * 1e4, hi * 1e4],
        "corr0": corr_eq[0], "corr0_ci": [float(clo), float(chi)],
        "significant": significant,
        "joint_sampling": significant,
    }
