"""Bring-your-own-data: inspect an uploaded CSV, map its columns, normalise it to the canonical hourly
schema and keep a registry of datasets (one active at a time). Live rows are appended here too."""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .data import REQUIRED

FLOWS = ["deposits_usd", "withdrawals_usd"]
MARKET = {   # column → (value used when the upload has no such column, label)
    "tier1_repo_rate_apy": (4.0, "Overnight repo rate, % a year"),
    "tier2_bond_yield_pct": (4.75, "Bond yield, %"),
    "tier2_bond_price_index": (100.0, "Bond price index"),
    "tier2_bond_mod_duration": (1.92, "Bond modified duration, years"),
    "tier3_stock_index_price": (100.0, "Equity index level"),
    "tier3_stock_volatility_pct": (18.0, "Equity volatility, %"),
    "tier1_early_redemption_haircut_pct": (0.5, "Repo early-redemption haircut, %"),
    "tier2_bond_liquidation_haircut_pct": (2.0, "Bond fire-sale haircut, %"),
    "tier3_stock_liquidation_haircut_pct": (15.0, "Equity fire-sale haircut, %"),
    "emergency_overdraft_penalty_apr_pct": (12.0, "Overdraft APR, %"),
}
FIELDS = {   # what the mapping screen offers
    "timestamp": "Date and time (required)",
    "deposits_usd": "Deposits / inflows",
    "withdrawals_usd": "Withdrawals / outflows",
    "net_flow_usd": "Net flow (inflows − outflows)",
    "repo_sweep_window_open": "Repo sweep window flag (else 17:00)",
    "repo_sweep_mature": "Repo maturity flag (else 08:00)",
    **{k: v[1] for k, v in MARKET.items()},
}
SYNONYMS = {
    "timestamp": ["timestamp", "datetime", "date_time", "date", "time", "ts", "period", "hour", "value_date", "posted_at", "booking_date"],
    "deposits_usd": ["deposits", "deposit", "inflow", "inflows", "credits", "credit", "receipts", "cash_in", "in", "collections"],
    "withdrawals_usd": ["withdrawals", "withdrawal", "outflow", "outflows", "debits", "debit", "payouts", "payments", "cash_out", "out", "disbursements"],
    "net_flow_usd": ["net_flow", "net", "netflow", "net_cash_flow", "amount", "signed_amount", "change"],
    "repo_sweep_window_open": ["sweep", "sweep_window", "repo_sweep_window_open"],
    "repo_sweep_mature": ["mature", "maturity", "repo_sweep_mature"],
    "tier1_repo_rate_apy": ["repo_rate", "repo", "overnight_rate", "sofr", "policy_rate"],
    "tier2_bond_yield_pct": ["bond_yield", "yield", "treasury_yield", "10y"],
    "tier2_bond_price_index": ["bond_price", "bond_index", "bond_price_index"],
    "tier3_stock_index_price": ["equity_index", "stock_index", "index", "spx", "equities", "stock_price"],
}
MIN_DAYS = 60
MAX_DAYS = 366 * 5


class DataError(ValueError):
    """A problem with the uploaded data that the user can fix."""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")


def read_csv(raw: bytes) -> pd.DataFrame:
    if not raw or not raw.strip():
        raise DataError("The file is empty")
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except Exception as e:
        raise DataError(f"Could not read the CSV: {e}") from e
    if df.shape[1] < 2:
        raise DataError("Found only one column. Is the file comma, semicolon or tab separated?")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def suggest_mapping(columns: list[str]) -> dict[str, str | None]:
    normed = {c: _norm(c) for c in columns}
    out: dict[str, str | None] = {}
    used: set[str] = set()
    for field in FIELDS:
        pick = next((c for c, n in normed.items() if n == field and c not in used), None)
        if not pick:
            for syn in SYNONYMS.get(field, []):
                pick = next((c for c, n in normed.items() if c not in used and (n == syn or n.startswith(syn + "_")
                             or n.endswith("_" + syn))), None)
                if pick:
                    break
        out[field] = pick
        if pick:
            used.add(pick)
    return out


def inspect(raw: bytes) -> dict:
    df = read_csv(raw)
    mapping = suggest_mapping(list(df.columns))
    return {"columns": list(df.columns), "rows": int(len(df)),
            "sample": df.head(6).astype(str).values.tolist(),
            "suggested": mapping, "fields": FIELDS,
            "dayfirst_guess": _guess_dayfirst(df[mapping["timestamp"]]) if mapping.get("timestamp") else True}


def _guess_dayfirst(s: pd.Series) -> bool:
    vals = s.astype(str).head(500)
    first = vals.str.extract(r"^(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}")
    if first.isna().all().all():
        return False           # ISO-like (year first)
    a, b = pd.to_numeric(first[0], errors="coerce"), pd.to_numeric(first[1], errors="coerce")
    if (a > 12).any():
        return True
    if (b > 12).any():
        return False
    return True


def parse_times(s: pd.Series, dayfirst: bool) -> pd.Series:
    s = s.astype(str).str.strip()
    fmts = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")
    for fmt in (("%d-%m-%Y %H:%M",) if dayfirst else ()) + fmts:
        t = pd.to_datetime(s, format=fmt, errors="coerce")
        if t.notna().mean() > 0.99:
            return t
    t = pd.to_datetime(s, errors="coerce", dayfirst=dayfirst, utc=True)
    if t.notna().mean() < 0.99:
        bad = s[t.isna()].head(3).tolist()
        raise DataError(f"Could not read {int(t.isna().sum())} timestamps, e.g. {bad}. Check the date column and day/month order.")
    return t.dt.tz_convert(None) if t.dt.tz is not None else t


def _num(s: pd.Series, name: str) -> pd.Series:
    blank = s.isna() | s.astype(str).str.strip().isin(["", "nan", "NaN", "None"])
    if not pd.api.types.is_numeric_dtype(s):
        s = s.astype(str).str.replace(r"[,$£€₹\s]", "", regex=True).str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    v = pd.to_numeric(s, errors="coerce")
    bad = int((v.isna() & ~blank).sum())
    if bad > 0.01 * len(v):
        raise DataError(f"{bad} values in '{name}' are not numbers")
    return v


def normalize(raw: bytes, mapping: dict, dayfirst: bool = True) -> tuple[pd.DataFrame, dict]:
    """Return (canonical hourly frame, report). Raises DataError with a fixable message."""
    src = read_csv(raw)
    mapping = {k: v for k, v in (mapping or {}).items() if v}
    missing = [c for c in mapping.values() if c not in src.columns]
    if missing:
        raise DataError(f"Mapped columns not in the file: {', '.join(missing)}")
    if "timestamp" not in mapping:
        raise DataError("Map the date/time column")
    has_dw = "deposits_usd" in mapping and "withdrawals_usd" in mapping
    if not has_dw and "net_flow_usd" not in mapping:
        raise DataError("Map deposits and withdrawals, or a single net-flow column")

    notes, assumed = [], []
    t = parse_times(src[mapping["timestamp"]], dayfirst)
    df = pd.DataFrame({"timestamp": t})
    if has_dw:
        df["deposits_usd"] = _num(src[mapping["deposits_usd"]], mapping["deposits_usd"]).fillna(0)
        df["withdrawals_usd"] = _num(src[mapping["withdrawals_usd"]], mapping["withdrawals_usd"]).fillna(0)
        neg_w = df["withdrawals_usd"] < 0
        if neg_w.mean() > 0.5:           # outflows recorded as negative numbers
            df["withdrawals_usd"] = df["withdrawals_usd"].abs()
            notes.append("Withdrawals were negative numbers; used their size")
        neg = int(((df["deposits_usd"] < 0) | (df["withdrawals_usd"] < 0)).sum())
        if neg:
            raise DataError(f"{neg} rows have negative deposits or withdrawals")
        if "net_flow_usd" in mapping:     # reconcile before aggregating, like the PRD's FR-01 check
            net = _num(src[mapping["net_flow_usd"]], mapping["net_flow_usd"])
            diff = (df["deposits_usd"] - df["withdrawals_usd"] - net).abs()
            tol = np.maximum(0.011, 1e-6 * df["deposits_usd"].abs())
            if (diff > tol).any():
                raise DataError(f"Net flow ≠ deposits − withdrawals on {int((diff > tol).sum())} rows "
                                f"(largest gap {diff.max():,.2f}). Fix the source or leave net flow unmapped.")
    else:
        net = _num(src[mapping["net_flow_usd"]], mapping["net_flow_usd"]).fillna(0)
        df["deposits_usd"], df["withdrawals_usd"] = net.clip(lower=0), (-net).clip(lower=0)
        notes.append("Only net flow given: deposits are its positive part, withdrawals its negative part")
    for col in MARKET:
        if col in mapping:
            df[col] = _num(src[mapping[col]], mapping[col])
    for col in ("repo_sweep_window_open", "repo_sweep_mature"):
        if col in mapping:
            df[col] = src[mapping[col]].astype(str).str.strip().str.upper().isin(["TRUE", "1", "YES", "Y", "T"])

    df = df.sort_values("timestamp")
    step = df["timestamp"].drop_duplicates().diff().median()
    if pd.notna(step) and step > pd.Timedelta(hours=1):
        raise DataError(f"Rows are about {step} apart. The engine works on hourly (or finer) data: "
                        "export hourly totals or individual transactions.")
    span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds() / 86400
    if span_days < MIN_DAYS:
        raise DataError(f"The data covers {span_days:.0f} days; at least {MIN_DAYS} are needed to train and test the models")
    if span_days > MAX_DAYS:
        raise DataError(f"The data covers {span_days:.0f} days; the limit is {MAX_DAYS} (five years)")

    # aggregate to clock hours: flows add up, prices take the last value in the hour
    raw_rows = len(df)
    df["timestamp"] = df["timestamp"].dt.floor("h")
    agg = {"deposits_usd": "sum", "withdrawals_usd": "sum"}
    agg.update({c: "last" for c in MARKET if c in df})
    agg.update({c: "max" for c in ("repo_sweep_window_open", "repo_sweep_mature") if c in df})
    h = df.groupby("timestamp").agg(agg)
    if raw_rows > len(h):
        notes.append(f"Combined {raw_rows:,} rows into {len(h):,} hourly totals")
    full = pd.date_range(h.index[0], h.index[-1], freq="h")
    gaps = len(full) - len(h)
    h = h.reindex(full)
    if gaps:
        notes.append(f"Filled {gaps:,} missing hours with zero flows")
    h[FLOWS] = h[FLOWS].fillna(0.0)
    for col, (default, _) in MARKET.items():
        if col in h:
            h[col] = h[col].ffill().bfill()
            if h[col].isna().all():
                h[col] = default
        else:
            h[col] = default
            assumed.append(col)
    hours = h.index.hour
    for col, hr in (("repo_sweep_window_open", 17), ("repo_sweep_mature", 8)):
        if col in h:
            h[col] = h[col].fillna(False).astype(bool)
        else:
            h[col] = hours == hr
            assumed.append(col)
    h["deposits_usd"] = h["deposits_usd"].round(2)
    h["withdrawals_usd"] = h["withdrawals_usd"].round(2)
    h["net_flow_usd"] = (h["deposits_usd"] - h["withdrawals_usd"]).round(2)
    h.index.name = "timestamp"
    out = h.reset_index()[REQUIRED]
    report = {"rows": int(len(out)), "source_rows": int(raw_rows), "start": str(out["timestamp"].iloc[0]),
              "end": str(out["timestamp"].iloc[-1]), "filled_hours": int(gaps), "assumed": assumed, "notes": notes}
    return out, report


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"]).dt.strftime("%Y-%m-%d %H:%M")
    for c in ("repo_sweep_window_open", "repo_sweep_mature"):
        d[c] = np.where(d[c].astype(bool), "TRUE", "FALSE")
    return d.to_csv(index=False, lineterminator="\n").encode()


def suggest_balances(df: pd.DataFrame) -> dict:
    """Starting balances in proportion to flow size (the demo uses ≈ 230 hours of average withdrawals as cash)."""
    w = float(df["withdrawals_usd"].mean()) or 1.0
    cash = _round_nice(230 * w)
    return {"cash": cash, "bonds": _round_nice(cash / 2), "equities": _round_nice(cash / 6)}


def _round_nice(x: float) -> float:
    if x <= 0:
        return 0.0
    mag = 10 ** max(0, int(np.floor(np.log10(x))) - 1)
    return float(round(x / mag) * mag)


class Registry:
    """datasets/registry.json lists every dataset; the demo file is registered in place, never modified."""

    def __init__(self, folder: Path, demo_csv: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "registry.json"
        self.demo_csv = demo_csv
        self.lock = threading.RLock()
        if not self.path.exists():
            self._save({"datasets": []})
        if demo_csv.exists() and not self.get("demo"):
            self._add({"id": "demo", "name": "Demo · treasury_4tier_dataset.csv (2026)", "file": str(demo_csv),
                       "rows": None, "readonly": True, "live": False, "created_by": "system", "created_at": _now(),
                       "assumed": [], "notes": [], "source": demo_csv.name})

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, reg: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _add(self, entry: dict):
        with self.lock:
            reg = self._load()
            reg["datasets"] = [d for d in reg["datasets"] if d["id"] != entry["id"]] + [entry]
            self._save(reg)

    def list(self) -> list[dict]:
        return self._load()["datasets"]

    def get(self, did: str) -> dict | None:
        return next((d for d in self.list() if d["id"] == did), None)

    def file(self, did: str) -> Path:
        d = self.get(did)
        if not d:
            raise DataError("No such dataset")
        return Path(d["file"]) if d.get("readonly") else self.folder / d["file"]

    def add_upload(self, df: pd.DataFrame, report: dict, name: str, source: str, by: str) -> dict:
        data = to_csv_bytes(df)
        did = hashlib.sha256(data).hexdigest()[:12]
        (self.folder / f"{did}.csv").write_bytes(data)
        entry = {"id": did, "name": (name or source or did)[:80], "file": f"{did}.csv", "rows": report["rows"],
                 "start": report["start"], "end": report["end"], "readonly": False, "live": False,
                 "created_by": by, "created_at": _now(), "assumed": report["assumed"], "notes": report["notes"],
                 "filled_hours": report["filled_hours"], "source": source[:120], "balances": suggest_balances(df)}
        self._add(entry)
        return entry

    def update(self, did: str, **fields):
        with self.lock:
            reg = self._load()
            for d in reg["datasets"]:
                if d["id"] == did:
                    d.update(fields)
            self._save(reg)

    def delete(self, did: str):
        d = self.get(did)
        if not d or d.get("readonly"):
            raise DataError("This dataset cannot be deleted")
        with self.lock:
            reg = self._load()
            reg["datasets"] = [x for x in reg["datasets"] if x["id"] != did]
            self._save(reg)
        try:
            (self.folder / d["file"]).unlink()
        except FileNotFoundError:
            pass

    # ---------- live rows ----------
    def append(self, did: str, rows: pd.DataFrame) -> dict:
        """Append canonical rows strictly after the last stored hour. Returns {added, last, gap_filled}."""
        d = self.get(did)
        if not d or d.get("readonly"):
            raise DataError("Live data can only be added to an uploaded dataset, not the read-only demo file")
        path = self.folder / d["file"]
        with self.lock:
            cur = pd.read_csv(path, usecols=["timestamp"])
            last = pd.to_datetime(cur["timestamp"].iloc[-1])
            rows = rows.sort_values("timestamp")
            if rows["timestamp"].iloc[0] <= last:
                raise DataError(f"Rows must be after the last stored hour ({last:%Y-%m-%d %H:%M}); got {rows['timestamp'].iloc[0]:%Y-%m-%d %H:%M}")
            if rows["timestamp"].duplicated().any():
                raise DataError("Duplicate hours in the batch")
            full = pd.date_range(last + pd.Timedelta(hours=1), rows["timestamp"].iloc[-1], freq="h")
            gap = len(full) - len(rows)
            if gap:          # an outage: keep the clock continuous; flows unknown → 0, prices carried forward
                rows = rows.set_index("timestamp").reindex(full)
                rows.index.name = "timestamp"
                rows = rows.reset_index()
                tail = pd.read_csv(path).iloc[-1]
                for c in MARKET:
                    rows[c] = rows[c].fillna(float(tail[c]))
                rows[FLOWS] = rows[FLOWS].fillna(0.0)
                hrs = rows["timestamp"].dt.hour
                rows["repo_sweep_window_open"] = rows["repo_sweep_window_open"].fillna(pd.Series(hrs == 17)).astype(bool)
                rows["repo_sweep_mature"] = rows["repo_sweep_mature"].fillna(pd.Series(hrs == 8)).astype(bool)
                rows["net_flow_usd"] = (rows["deposits_usd"] - rows["withdrawals_usd"]).round(2)
            with open(path, "ab") as f:
                f.write(to_csv_bytes(rows[REQUIRED]).split(b"\n", 1)[1])
            n = int(d.get("rows") or len(cur)) + len(rows)
            new_last = rows["timestamp"].iloc[-1]
            self.update(did, rows=n, end=str(new_last), live=True)
        return {"added": int(len(rows)), "gap_filled": int(gap), "last": f"{new_last:%Y-%m-%d %H:%M}", "rows": n}


def live_rows(payload, tail: dict | None = None) -> pd.DataFrame:
    """Validate incoming live rows (JSON list of objects) into canonical columns.
    Market fields not sent are carried forward from the last stored row."""
    if not isinstance(payload, list) or not payload:
        raise DataError("Send a non-empty list of rows")
    if len(payload) > 24 * 31:
        raise DataError("At most 744 rows (one month) per request")
    df = pd.DataFrame(payload)
    if "timestamp" not in df:
        raise DataError("Each row needs a timestamp")
    df["timestamp"] = parse_times(df["timestamp"], dayfirst=False).dt.floor("h")
    if "deposits_usd" not in df or "withdrawals_usd" not in df:
        if "net_flow_usd" in df:
            net = pd.to_numeric(df["net_flow_usd"], errors="coerce")
            df["deposits_usd"], df["withdrawals_usd"] = net.clip(lower=0), (-net).clip(lower=0)
        else:
            raise DataError("Each row needs deposits_usd and withdrawals_usd (or net_flow_usd)")
    for c in FLOWS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
        if df[c].isna().any() or (df[c] < 0).any():
            raise DataError(f"{c} must be a non-negative number in every row")
    df = df.groupby("timestamp", as_index=False).agg({**{c: "sum" for c in FLOWS},
                                                       **{c: "last" for c in df.columns if c in MARKET or c.startswith("repo_sweep")}})
    for c, (default, _) in MARKET.items():
        prev = float(tail[c]) if tail and c in tail else default
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(prev) if c in df else prev
    hrs = df["timestamp"].dt.hour
    for c, hr in (("repo_sweep_window_open", 17), ("repo_sweep_mature", 8)):
        df[c] = df[c].astype(str).str.upper().isin(["TRUE", "1", "YES"]) if c in df else hrs == hr
    df["deposits_usd"], df["withdrawals_usd"] = df["deposits_usd"].round(2), df["withdrawals_usd"].round(2)
    df["net_flow_usd"] = (df["deposits_usd"] - df["withdrawals_usd"]).round(2)
    return df[REQUIRED]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
