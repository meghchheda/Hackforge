"""Digital Twin (FR-11) and reverse stress test (FR-17)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .treasury import Market, Treasury

AXES = {
    "withdrawal_mult": {"label": "Withdrawal multiplier", "lo": 1.0, "hi": 5.0, "unit": "×"},
    "deposit_delay_h": {"label": "Deposit delay", "lo": 0.0, "hi": 48.0, "unit": "h"},
    "equity_drop": {"label": "Equity drawdown", "lo": 0.0, "hi": 0.5, "unit": "%"},
    "yield_shock_bp": {"label": "Bond yield shock", "lo": 0.0, "hi": 300.0, "unit": "bp"},
}


def state_at(run: dict, t: int) -> dict:
    """Ledger state at the end of hour t − 1 from a replay."""
    i = max(0, t - 1 - run["start"])
    r = run["rec"]
    return {"cash": float(r["cash"][i]), "repo": float(r["repo"][i]), "bonds": float(r["bonds"][i]),
            "equities": float(r["equities"][i]), "od": float(r["od"][i]), "repo_rate": None}


def extend(df: pd.DataFrame, hours: int) -> pd.DataFrame:
    """Append `hours` projected rows: hour-of-week average flows from the last eight weeks, market values held flat.
    Used when a what-if runs past the newest observed hour (live mode)."""
    if hours <= 0:
        return df
    recent = df.iloc[-24 * 56:]
    dep = recent.groupby("how")["deposits_usd"].mean()
    wd = recent.groupby("how")["withdrawals_usd"].mean()
    last = df.iloc[-1]
    ts = pd.date_range(last["timestamp"] + pd.Timedelta(hours=1), periods=hours, freq="h")
    fut = pd.DataFrame({"timestamp": ts})
    fut["hour"], fut["dow"] = ts.hour, ts.dayofweek
    fut["how"] = fut["dow"] * 24 + fut["hour"]
    fut["deposits_usd"] = fut["how"].map(dep).fillna(float(recent["deposits_usd"].mean())).values
    fut["withdrawals_usd"] = fut["how"].map(wd).fillna(float(recent["withdrawals_usd"].mean())).values
    fut["net_flow_usd"] = fut["deposits_usd"] - fut["withdrawals_usd"]
    fut["repo_sweep_window_open"], fut["repo_sweep_mature"] = fut["hour"] == 17, fut["hour"] == 8
    for c in df.columns:
        if c not in fut:
            fut[c] = last[c]
    return pd.concat([df, fut[df.columns]], ignore_index=True)


def twin(tr: Treasury, df, cfg: dict, balances: dict, base_run: dict, shocks: dict) -> dict:
    start = int(shocks["start"])
    window = min(int(shocks.get("hours", 72)) + 48, 240)
    n_obs = len(df)
    use = extend(df, start + window - n_obs)          # projected hours only when the window runs past the data
    init = state_at(base_run, start)
    init["repo_rate"] = float(tr.base_market.repo[max(0, start - 1)])
    shocked = tr.simulate("optimized", cfg, balances, start, start + window, market=Market(use, shocks),
                          init=init, breaker=True)
    base = tr.simulate("optimized", cfg, balances, start, start + window, init=init, breaker=True,
                       market=Market(use) if len(use) > n_obs else None)
    ts = use["timestamp"].iloc[start:start + window].dt.strftime("%m-%d %H:%M").tolist()

    def series(run):
        r = run["rec"]
        return {"cash": (r["cash"] / 1e6).round(1).tolist(), "od": (r["od"] / 1e6).round(1).tolist(),
                "reserve": (r["reserve"] / 1e6).round(1).tolist(), "repo": (r["repo"] / 1e6).round(1).tolist()}

    return {"ts": ts, "shocked": series(shocked), "baseline": series(base),
            "metrics": {"shocked": shocked["metrics"], "baseline": base["metrics"]},
            "alerts": shocked["alerts"], "breaker_hours": int(shocked["breaker"].sum()), "shocks": shocks,
            "projected_from": (n_obs - start) if len(use) > n_obs else None}


def _fails(tr, df, cfg, balances, init, start, window, shocks) -> tuple[bool, float]:
    s = dict(shocks, start=start, hours=window)
    run = tr.simulate("optimized", cfg, balances, start, start + window, market=Market(df, s),
                      init=init, fast=False, breaker=False)
    mt = run["metrics"]
    return (mt["forced_sales"] > 1 or mt["shortfall_hours"] > 0), mt["haircut_cost"]


def reverse_stress(tr: Treasury, df, cfg: dict, balances: dict, base_run: dict, x_axis: str = "withdrawal_mult",
                   y_axis: str = "deposit_delay_h", start: int | None = None, window: int = 72, n: int = 16) -> dict:
    """Find the smallest shocks that force a fire sale or a payout gap (beyond cash, repo and overdraft)."""
    if start is None:
        liquid = base_run["rec"]["cash"] + base_run["rec"]["repo"]
        start = base_run["start"] + int(np.argmin(liquid[:-window]))
    init = state_at(base_run, start)
    init["repo_rate"] = float(tr.base_market.repo[max(0, start - 1)])
    ax, ay = AXES[x_axis], AXES[y_axis]
    xs = np.linspace(ax["lo"], ax["hi"], n)
    ys = np.linspace(ay["lo"], ay["hi"], n)
    grid = []
    for y in ys:
        row = []
        for x in xs:
            f, _ = _fails(tr, df, cfg, balances, init, start, window, {x_axis: x, y_axis: y})
            row.append(bool(f))
        grid.append(row)
    # boundary by bisection on the x axis for each y
    boundary = []
    for y in ys:
        lo, hi = ax["lo"], ax["hi"]
        if _fails(tr, df, cfg, balances, init, start, window, {x_axis: lo, y_axis: y})[0]:
            boundary.append(lo); continue
        if not _fails(tr, df, cfg, balances, init, start, window, {x_axis: hi, y_axis: y})[0]:
            boundary.append(None); continue
        tol = 0.05 if x_axis == "withdrawal_mult" else (ax["hi"] - ax["lo"]) / 100
        while hi - lo > tol:
            mid = (lo + hi) / 2
            if _fails(tr, df, cfg, balances, init, start, window, {x_axis: mid, y_axis: y})[0]:
                hi = mid
            else:
                lo = mid
        boundary.append(round(hi, 3))
    # confirm three boundary points with full Digital Twin runs
    confirmed = []
    for j in (0, n // 2, n - 1):
        b = boundary[j]
        if b is None:
            continue
        s = {x_axis: b, y_axis: float(ys[j]), "start": start, "hours": window}
        run = tr.simulate("optimized", cfg, balances, start, start + window, market=Market(df, s), init=init,
                          breaker=False)
        confirmed.append({"x": b, "y": float(ys[j]),
                          "fails": run["metrics"]["forced_sales"] > 1 or run["metrics"]["shortfall_hours"] > 0})
    margin = boundary[0]
    return {"x_axis": x_axis, "y_axis": y_axis, "x_label": ax["label"], "y_label": ay["label"],
            "xs": xs.round(3).tolist(), "ys": ys.round(3).tolist(), "grid": grid, "boundary": boundary,
            "start": start, "start_ts": df["timestamp"].iloc[start].strftime("%Y-%m-%d %H:%M"),
            "window": window, "margin": margin, "confirmed": confirmed, "x_unit": ax["unit"], "y_unit": ay["unit"],
            "start_state": init}
