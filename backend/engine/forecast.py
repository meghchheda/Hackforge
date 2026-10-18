"""Forecasting (FR-03) with split-conformal intervals and adaptive conformal coverage monitoring (FR-19)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

HORIZONS = (1, 6, 24)
TARGETS = {"net": "net_flow_usd", "deposits": "deposits_usd", "withdrawals": "withdrawals_usd"}
LEVELS = (0.80, 0.95, 0.99)
MODEL_VERSION = "hgb-q50-conformal-v1"


def split_bounds(df: pd.DataFrame) -> dict[str, str]:
    """First two thirds train, next sixth calibrates, last sixth tests; cut at midnight.
    For the 2026 demo file this gives Jan–Aug / Sep–Oct / Nov–Dec."""
    ts = df["timestamp"]
    n = len(df)
    cal = ts.iloc[int(n * 2 / 3)].normalize()
    test = ts.iloc[int(n * 5 / 6)].normalize()
    return {"cal": cal.strftime("%Y-%m-%d %H:%M"), "test": test.strftime("%Y-%m-%d %H:%M")}


def splits(df: pd.DataFrame, bounds: dict | None = None) -> tuple[dict[str, np.ndarray], dict]:
    bounds = bounds or split_bounds(df)
    ts = df["timestamp"]
    cal, test = pd.Timestamp(bounds["cal"]), pd.Timestamp(bounds["test"])
    idx = {"train": np.where(ts < cal)[0], "cal": np.where((ts >= cal) & (ts < test))[0], "test": np.where(ts >= test)[0]}
    return idx, bounds


def features(df: pd.DataFrame, col: str, h: int) -> pd.DataFrame:
    """Row t holds information known at hour t, used to predict col at t + h."""
    y = df[col]
    tgt_ts = df["timestamp"] + pd.Timedelta(hours=h)
    X = pd.DataFrame({
        "hour": tgt_ts.dt.hour,
        "dow": tgt_ts.dt.dayofweek,
        "dom": tgt_ts.dt.day,
        "lag0": y,
        "lag1": y.shift(1),
        "lag2": y.shift(2),
        "season24": y.shift(24 - h),           # same hour yesterday, relative to the target
        "season168": y.shift(168 - h),         # same hour last week
        "roll6": y.rolling(6).mean(),
        "roll24": y.rolling(24).mean(),
        "roll168": y.rolling(168).mean(),
        "std24": y.rolling(24).std(),
        "eq_ret24": df["tier3_stock_index_price"].pct_change(24),
        "repo": df["tier1_repo_rate_apy"],
        "yield": df["tier2_bond_yield_pct"],
    })
    return X


def _wape(y, p):
    return float(np.abs(y - p).sum() / np.abs(y).sum())


def _mae(y, p):
    return float(np.abs(y - p).mean())


class Forecaster:
    """Fits (or reloads) one median model per target and horizon, then scores them. `models` + `bounds` from an
    earlier fit can be passed in to re-score on newer data without refitting (live updates)."""

    def __init__(self, df: pd.DataFrame, bounds: dict | None = None, models: dict | None = None,
                 cache_dir: Path | None = None):
        self.df = df
        self.idx, self.bounds = splits(df, bounds)
        if min(len(v) for v in self.idx.values()) < 24 * 7:
            raise ValueError("Each of the train, calibration and test windows needs at least a week of data")
        self.metrics: list[dict] = []
        self.bands: dict = {}
        self.coverage: dict = {}
        self.cached = False
        self.models = models or self._load_or_fit(cache_dir)
        self._score()

    def _cache_key(self) -> str:
        cut = self.idx["cal"][0]
        h = hashlib.sha256(f"{MODEL_VERSION}|{self.bounds}".encode())
        h.update(self.df["timestamp"].iloc[:cut].astype("int64").to_numpy().tobytes())
        h.update(self.df[["tier3_stock_index_price", "tier1_repo_rate_apy", "tier2_bond_yield_pct", *TARGETS.values()]]
                 .iloc[:cut].to_numpy(dtype="float64").tobytes())
        return h.hexdigest()[:24]

    def _load_or_fit(self, cache_dir: Path | None) -> dict:
        path = cache_dir / f"forecast-{self._cache_key()}.joblib" if cache_dir else None
        if path and path.exists():
            try:
                models = joblib.load(path)       # only files this service wrote into its own data folder
                self.cached = True
                return models
            except Exception:
                path.unlink(missing_ok=True)
        models = self._fit()
        if path:
            cache_dir.mkdir(parents=True, exist_ok=True)
            joblib.dump(models, path)
            for old in sorted(cache_dir.glob("forecast-*.joblib"), key=lambda p: p.stat().st_mtime)[:-6]:
                old.unlink(missing_ok=True)    # keep the six most recent fits
        return models

    def _fit(self) -> dict:
        df, idx, models = self.df, self.idx, {}
        for key, col in TARGETS.items():
            for h in HORIZONS:
                X = features(df, col, h)
                y = df[col].shift(-h)                       # value at t + h
                ok = X.notna().all(axis=1) & y.notna()
                tr = np.intersect1d(idx["train"], np.where(ok)[0])
                tr = tr[tr + h < idx["cal"][0]]             # targets stay inside train
                model = HistGradientBoostingRegressor(
                    loss="quantile", quantile=0.5, max_iter=250, learning_rate=0.05,
                    max_leaf_nodes=31, random_state=7)
                model.fit(X.iloc[tr], y.iloc[tr])
                models[(key, h)] = model
        return models

    def _score(self):
        df, idx = self.df, self.idx
        n = len(df)
        for key, col in TARGETS.items():
            for h in HORIZONS:
                X = features(df, col, h)
                y = df[col].shift(-h)
                ok = X.notna().all(axis=1) & y.notna()
                model = self.models[(key, h)]
                pred = pd.Series(np.nan, index=df.index)
                valid = np.where(ok)[0]
                pred.iloc[valid] = model.predict(X.iloc[valid])

                te = np.intersect1d(idx["test"], valid)
                te = te[te + h < n]
                yt = y.iloc[te].values
                naive24 = df[col].shift(24 - h).iloc[te].values
                med24 = df[col].rolling(24).median().iloc[te].values
                self.metrics.append({
                    "target": key, "h": h,
                    "model_wape": _wape(yt, pred.iloc[te].values),
                    "naive_wape": _wape(yt, naive24),
                    "median_wape": _wape(yt, med24),
                    "model_mae": _mae(yt, pred.iloc[te].values),
                    "naive_mae": _mae(yt, naive24),
                })

                if key == "net":
                    ca = np.intersect1d(idx["cal"], valid)
                    ca = ca[ca + h < idx["test"][0]]
                    scores = np.abs(y.iloc[ca].values - pred.iloc[ca].values)
                    qs = {lv: _conformal_q(scores, lv) for lv in LEVELS}
                    self.bands[h] = {"pred": pred, "y": y, "q": qs, "scores": scores, "test": te}
                    self.coverage[h] = self._coverage(yt, pred.iloc[te].values, qs, scores)

    def _coverage(self, y, p, qs, scores, gamma=0.005, target=0.95):
        """Static split-conformal coverage plus adaptive conformal inference (ACI) on the test months."""
        err = np.abs(y - p)
        static_hit = err <= qs[0.95]
        alpha_t, aci_hit, widths = 1 - target, [], []
        sorted_scores = np.sort(scores)
        for e in err:
            lvl = min(max(1 - alpha_t, 0.5), 0.9995)
            w = float(np.quantile(sorted_scores, lvl))
            hit = e <= w
            aci_hit.append(hit)
            widths.append(w)
            alpha_t = alpha_t + gamma * ((1 - target) - (0 if hit else 1))
        out = {}
        for lv in LEVELS:
            out[f"static_{int(lv*100)}"] = float((err <= qs[lv]).mean())
        out["aci_95"] = float(np.mean(aci_hit))
        out["rolling_static"] = pd.Series(static_hit).rolling(168, min_periods=168).mean().round(4).tolist()
        out["rolling_aci"] = pd.Series(aci_hit).rolling(168, min_periods=168).mean().round(4).tolist()
        return out

    # ---------- public views ----------
    def series(self, h: int, start: int | None = None, hours: int = 24 * 14) -> dict:
        b = self.bands[h]
        te = b["test"]
        if start is None:
            start = 0
        sel = te[start:start + hours]
        tgt_ts = (self.df["timestamp"].iloc[sel] + pd.Timedelta(hours=h)).dt.strftime("%Y-%m-%d %H:%M")
        p = b["pred"].iloc[sel].values
        return {
            "ts": tgt_ts.tolist(),
            "actual": (b["y"].iloc[sel].values / 1e6).round(3).tolist(),
            "p50": (p / 1e6).round(3).tolist(),
            "lo95": ((p - b["q"][0.95]) / 1e6).round(3).tolist(),
            "hi95": ((p + b["q"][0.95]) / 1e6).round(3).tolist(),
            "lo99": ((p - b["q"][0.99]) / 1e6).round(3).tolist(),
            "hi99": ((p + b["q"][0.99]) / 1e6).round(3).tolist(),
        }

    def coverage_view(self, h: int) -> dict:
        c = self.coverage[h]
        ts = self.df["timestamp"].iloc[self.bands[h]["test"]].dt.strftime("%Y-%m-%d %H:%M").tolist()
        return {**{k: v for k, v in c.items() if not k.startswith("rolling")},
                "ts": ts[::6], "rolling_static": c["rolling_static"][::6], "rolling_aci": c["rolling_aci"][::6]}

    def drift_test(self, h: int = 1, factor: float = 2.0, inject_at: int = 24 * 14) -> dict:
        """Inflate test residuals by `factor` from `inject_at`; report hours until the coverage alarm trips."""
        b = self.bands[h]
        te = b["test"]
        inject_at = min(inject_at, max(24, len(te) // 3))
        y = b["y"].iloc[te].values
        p = b["pred"].iloc[te].values
        err = (y - p).copy()
        err[inject_at:] *= factor
        hit = np.abs(err) <= b["q"][0.95]
        r24 = pd.Series(hit).rolling(24, min_periods=24).mean()
        r168 = pd.Series(hit).rolling(168, min_periods=48).mean()
        alarm = (r24 < 0.75) | (r168 < 0.93)
        after = np.where(alarm.values & (np.arange(len(hit)) >= inject_at))[0]
        trip = int(after[0] - inject_at) if len(after) else None
        lo = max(0, inject_at - 72)
        return {"factor": factor, "hours_to_alarm": trip,
                "rolling24": r24.round(3).fillna(1).tolist()[lo: inject_at + 120],
                "inject_offset": inject_at - lo}

    def point(self, t: int, h: int, key: str = "net") -> float:
        X = features(self.df, TARGETS[key], h).iloc[[t]]
        if X.isna().any(axis=None):
            return float(self.df[TARGETS[key]].iloc[t])
        return float(self.models[(key, h)].predict(X)[0])

    def band_q(self, h: int, level: float) -> float:
        return float(self.bands[h]["q"][level])


def _conformal_q(scores: np.ndarray, level: float) -> float:
    n = len(scores)
    k = min(int(np.ceil((n + 1) * level)), n)
    return float(np.sort(scores)[k - 1])
