"""Dataset ingestion, validation (FR-01) and the dataset honesty report (FR-25)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = [
    "timestamp", "deposits_usd", "withdrawals_usd", "net_flow_usd",
    "repo_sweep_window_open", "repo_sweep_mature", "tier1_repo_rate_apy",
    "tier2_bond_yield_pct", "tier2_bond_price_index", "tier2_bond_mod_duration",
    "tier3_stock_index_price", "tier3_stock_volatility_pct",
    "tier1_early_redemption_haircut_pct", "tier2_bond_liquidation_haircut_pct",
    "tier3_stock_liquidation_haircut_pct", "emergency_overdraft_penalty_apr_pct",
]
FLAGS = ["repo_sweep_window_open", "repo_sweep_mature"]


def load(path: Path) -> tuple[pd.DataFrame, dict]:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    df = pd.read_csv(path)

    checks = []
    missing_cols = [c for c in REQUIRED if c not in df.columns]
    checks.append(_check("Required columns", not missing_cols,
                         "All 16 present" if not missing_cols else f"Missing: {', '.join(missing_cols)}"))
    if missing_cols:
        raise ValueError(f"Missing columns: {missing_cols}")

    ts = pd.to_datetime(df["timestamp"], format="%d-%m-%Y %H:%M", errors="coerce")     # demo file
    if ts.isna().any():
        ts = pd.to_datetime(df["timestamp"], format="%Y-%m-%d %H:%M", errors="coerce")  # normalised uploads
    if ts.isna().any():
        raise ValueError(f"Unreadable timestamps, e.g. {df.loc[ts.isna(), 'timestamp'].head(2).tolist()}")
    df["timestamp"] = ts
    for c in FLAGS:
        df[c] = df[c].astype(str).str.upper().eq("TRUE")
    df = df.sort_values("timestamp").reset_index(drop=True)

    n_missing = int(df[REQUIRED].isna().sum().sum())
    checks.append(_check("Missing values", n_missing == 0, f"{n_missing} missing cells"))

    dupes = int(df["timestamp"].duplicated().sum())
    checks.append(_check("Unique timestamps", dupes == 0, f"{dupes} duplicates"))

    gaps = df["timestamp"].diff().dropna()
    non_hourly = int((gaps != pd.Timedelta(hours=1)).sum())
    checks.append(_check("Hourly continuity", non_hourly == 0,
                         f"{len(df):,} rows, {non_hourly} gaps"))

    diff = (df["deposits_usd"] - df["withdrawals_usd"] - df["net_flow_usd"]).abs()
    max_diff = float(diff.max())
    tol = 0.01 + 1e-6      # one cent, allowing for float representation of rounded values
    checks.append(_check("Net-flow invariant", max_diff <= tol,
                         f"Max |deposits − withdrawals − net| = ${max_diff:.2f} (cent rounding)"))

    neg = int((df[["deposits_usd", "withdrawals_usd"]] < 0).sum().sum())
    checks.append(_check("Non-negative flows", neg == 0, f"{neg} negative values"))

    if max_diff > tol:
        raise ValueError("Net-flow invariant failed; run blocked")

    df["hour"] = df["timestamp"].dt.hour
    df["dow"] = df["timestamp"].dt.dayofweek
    df["how"] = df["dow"] * 24 + df["hour"]
    meta = {
        "sha256": sha,
        "rows": len(df),
        "start": df["timestamp"].iloc[0].isoformat(),
        "end": df["timestamp"].iloc[-1].isoformat(),
        "checks": checks,
    }
    return df, meta


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def summary(df: pd.DataFrame) -> dict:
    nf = df["net_flow_usd"]
    acf = {lag: float(nf.autocorr(lag)) for lag in (1, 2, 3, 6, 12, 24, 48, 168)}
    heat = (df.groupby(["dow", "hour"])["net_flow_usd"].mean() / 1e6).unstack().round(3)
    p05 = float(nf.quantile(0.05))
    return {
        "avg_deposits": float(df["deposits_usd"].mean()),
        "avg_withdrawals": float(df["withdrawals_usd"].mean()),
        "cum_net": float(nf.sum()),
        "neg_hours": int((nf < 0).sum()),
        "neg_share": float((nf < 0).mean()),
        "p05_net": p05,
        "min_net": float(nf.min()),
        "acf": acf,
        "heatmap": heat.values.tolist(),
        "hist": _hist(nf / 1e6, 40),
        "daily": {
            "date": df["timestamp"].dt.strftime("%Y-%m-%d").iloc[::24].tolist(),
            "net": (nf.groupby(df["timestamp"].dt.date).sum() / 1e6).round(2).tolist(),
            "equity": df["tier3_stock_index_price"].iloc[::24].round(1).tolist(),
            "bond_yield": df["tier2_bond_yield_pct"].iloc[::24].round(3).tolist(),
            "repo": df["tier1_repo_rate_apy"].iloc[::24].round(3).tolist(),
        },
    }


def _hist(s: pd.Series, bins: int) -> dict:
    counts, edges = np.histogram(s, bins=bins)
    return {"edges": np.round(edges, 2).tolist(), "counts": counts.tolist()}


def honesty_report(df: pd.DataFrame) -> dict:
    """FR-25: signs of generation and what the data can and cannot support."""
    findings = []
    const_cols = [c for c in REQUIRED[1:] if df[c].nunique() == 1]
    findings.append({
        "check": "Constant columns",
        "result": f"{len(const_cols)} of 15 value columns never change",
        "detail": ", ".join(const_cols),
        "flag": len(const_cols) > 0,
    })
    n_days = int(df["timestamp"].dt.normalize().nunique())
    for c, h in (("repo_sweep_window_open", 17), ("repo_sweep_mature", 8)):
        hours = sorted(df.loc[df[c], "hour"].unique().tolist())
        days = int(df.loc[df[c]].shape[0])
        findings.append({
            "check": f"{c} regularity",
            "result": f"True on {days} of {n_days} days, only at {', '.join(f'{x:02d}:00' for x in hours) or 'no hour'}",
            "detail": "Perfectly regular calendar flag; operational meaning unconfirmed",
            "flag": days == n_days and hours == [h],
        })
    diff = (df["deposits_usd"] - df["withdrawals_usd"] - df["net_flow_usd"]).abs().max()
    findings.append({
        "check": "Invariant exactness",
        "result": f"net_flow = deposits − withdrawals to within ${diff:.2f}",
        "detail": "Exact to the cent: consistent with generated rather than reconciled data",
        "flag": diff < 0.011,
    })
    daily = df.pivot_table(index=df["timestamp"].dt.date, columns="hour", values="deposits_usd")
    prof = daily.div(daily.mean(axis=1), axis=0)
    corr = float(np.corrcoef(prof.values)[np.triu_indices(len(prof), 1)].mean())
    findings.append({
        "check": "Repeating daily shape",
        "result": f"Average correlation between days' intraday deposit shapes = {corr:.2f}",
        "detail": "High values suggest one template curve plus noise",
        "flag": corr > 0.8,
    })
    cents = (df["deposits_usd"] * 100 % 1).abs().max()
    findings.append({
        "check": "Rounding",
        "result": "All flows have exactly 2 decimals" if cents < 1e-6 else "Mixed precision",
        "detail": "Uniform precision is typical of generators",
        "flag": cents < 1e-6,
    })
    eq = df["tier3_stock_index_price"]
    findings.append({
        "check": "Equity path",
        "result": f"Index moves {eq.iloc[0]:,.0f} → {eq.iloc[-1]:,.0f} ({eq.iloc[-1]/eq.iloc[0]-1:+.1%}) over the year",
        "detail": "Single path: one market regime, so equity results are path-dependent",
        "flag": True,
    })
    return {
        "findings": findings,
        "supports": [
            "Comparing policies on this exact year of flows and prices",
            "Testing forecast calibration on held-out months of this file",
            "Showing how timing (17:00 sweep, 08:00 maturity) changes liquidity risk",
            "Stress and what-if analysis around these observed patterns",
        ],
        "does_not_support": [
            "Claims about real-world treasury performance or predictive accuracy",
            f"Validation of rare annual events ({n_days} days of history)",
            "Any statement about actual balances, holdings or realized returns",
            "Learning volatility, duration or haircuts (they are constants)",
        ],
    }
