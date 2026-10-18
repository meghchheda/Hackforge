"""Application state: loads the dataset, fits models, runs replays and serves views to the API."""
from __future__ import annotations

import copy
import hashlib
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import data as data_mod
from .audit import Store, canonical
from .forecast import MODEL_VERSION, Forecaster
from .scenarios import ScenarioEngine, wrong_way
from .stress import reverse_stress, state_at, twin
from .treasury import DEFAULT_BALANCES, DEFAULT_POLICY, POLICIES, Treasury, od_limit

ROOT = Path(__file__).resolve().parents[2]
DEMO_DEFAULT_HOUR = "2026-11-16 09:00"


class App:
    """Everything computed from one dataset: models, scenario tables, replays and the views the API serves.
    Built in the background by the engine manager; `prev` lets a live update reuse fitted models."""

    def __init__(self, csv: Path, store: Store | None = None, dataset: dict | None = None,
                 cache_dir: Path | None = None, prev: "App | None" = None, progress=None):
        t0 = time.time()
        say = progress or (lambda msg: None)
        self.dataset = dataset or {"id": "demo", "name": csv.name, "live": False, "assumed": []}
        self.live = bool(self.dataset.get("live"))
        say("Checking the data…")
        self.df, self.meta = data_mod.load(csv)
        self.summary = data_mod.summary(self.df)
        self.honesty = data_mod.honesty_report(self.df)
        if self.dataset.get("assumed"):
            self.honesty["findings"].insert(0, {
                "check": "Assumed columns", "flag": True,
                "result": f"{len(self.dataset['assumed'])} columns were not in the upload and use standard values",
                "detail": ", ".join(self.dataset["assumed"])})
        say("Re-scoring the forecast models…" if prev else "Training the forecast models…")
        self.fc = Forecaster(self.df, bounds=prev.fc.bounds if prev else None, models=prev.fc.models if prev else None,
                             cache_dir=cache_dir)
        self.fitted_rows = prev.fitted_rows if prev else len(self.df)
        self.fitted_at = prev.fitted_at if prev else time.time()
        self.test_start = int(self.fc.idx["test"][0])
        say("Building outflow scenarios…")
        self.scen = ScenarioEngine(self.df, pool_end=self.test_start)
        self.wrongway = wrong_way(self.df)
        self.scen.joint = self.wrongway["joint_sampling"]
        self.store = store or Store(ROOT / "data" / "reserve_juggler.db")
        if not self.store.current_policy():
            self.store.save_policy(copy.deepcopy(DEFAULT_POLICY), "system")
        self.balances = self.store.get_kv(self._bal_key()) or self.dataset.get("balances") or dict(DEFAULT_BALANCES)
        self.flags, self.reasons, self.psi = self._breaker_flags(self.policy)
        self.tr = Treasury(self.df, self.scen, self.flags, self.reasons, train_end=int(self.fc.idx["cal"][0]))
        self.lock = threading.Lock()
        self._cache: dict = {}
        self.stale_reason: str | None = None       # set by the live feed monitor
        self.ts_str = self.df["timestamp"].dt.strftime("%Y-%m-%d %H:%M").tolist()
        pre = self.df.iloc[:self.test_start]
        self.prof_dep = pre.groupby("how")["deposits_usd"].mean()
        self.prof_wd = pre.groupby("how")["withdrawals_usd"].mean()
        say("Replaying every policy…")
        self.replays()
        self.backtest()
        self.boot_seconds = round(time.time() - t0, 1)

    def _bal_key(self) -> str:
        return f"balances:{self.dataset['id']}"

    @property
    def default_t(self) -> int:
        n = len(self.df)
        if self.live:
            return n - 1
        if self.dataset.get("id") == "demo" and DEMO_DEFAULT_HOUR in self.ts_str:
            return self.ts_str.index(DEMO_DEFAULT_HOUR)
        t = min(self.test_start + 15 * 24, n - 1)
        t -= (int(self.df["hour"].iloc[t]) - 9) % 24        # back to 09:00 that day (or the day before)
        return int(max(t, 0))

    # ---------- policy ----------
    @property
    def policy_rec(self) -> dict:
        return self.store.current_policy()

    @property
    def policy(self) -> dict:
        return self.policy_rec["policy"]

    def save_policy(self, policy: dict, user: str) -> dict:
        rec = self.store.save_policy(policy, user)
        self._cache.clear()
        self.flags, self.reasons, self.psi = self._breaker_flags(rec["policy"])
        self.tr.breaker_flags, self.tr.breaker_reasons = self.flags, self.reasons
        return rec

    def save_policy_refresh(self):
        """Pick up a policy that another engine instance saved."""
        self._cache.clear()
        self.flags, self.reasons, self.psi = self._breaker_flags(self.policy)
        self.tr.breaker_flags, self.tr.breaker_reasons = self.flags, self.reasons

    def set_balances(self, balances: dict, user: str) -> dict:
        b = {k: float(balances[k]) for k in ("cash", "bonds", "equities")}
        if any(v < 0 or not np.isfinite(v) for v in b.values()):
            raise ValueError("Balances must be finite and non-negative")
        self.balances = b
        self._cache.clear()
        self.store.set_kv(self._bal_key(), b)
        self.store.log("balances_set", {"balances": self.balances, "dataset": self.dataset["id"], "by": user})
        return self.balances

    def _key(self, *extra) -> str:
        return canonical([self.policy_rec["version"], self.balances, *extra])

    def project_cash(self, t: int, k: int, cash: float) -> float:
        """Cash at hour k (> t) using expected hour-of-week flows."""
        how = int(self.df["how"].iloc[t])
        return cash + (float(self.tr.prof_cum[how][k - t - 1]) if k > t else 0.0)

    def next_sweep_hour(self, t: int) -> tuple[int, int, bool]:
        """(index of the next 17:00 sweep at or after t, its hour-of-week, projected?). Projected when it lies beyond
        the newest row, which happens in live mode before 17:00."""
        n = len(self.df)
        k = next((x for x in range(t, min(t + 24, n)) if self.df["repo_sweep_window_open"].iloc[x]), None)
        if k is not None:
            return k, int(self.df["how"].iloc[k]), False
        hour = int(self.df["hour"].iloc[t])
        ahead = (17 - hour) % 24
        return t + ahead, (int(self.df["how"].iloc[t]) + ahead) % 168, True

    # ---------- circuit breaker inputs (FR-10) ----------
    def _breaker_flags(self, cfg: dict):
        n = len(self.df)
        flags = np.zeros(n, bool)
        reasons = [""] * n
        cb = cfg["circuit_breaker"]
        # coverage drift on the 1 h net-flow band (test months)
        b = self.fc.bands[1]
        te = b["test"]
        hit = np.abs(b["y"].iloc[te].values - b["pred"].iloc[te].values) <= b["q"][0.95]
        r24 = pd.Series(hit).rolling(24, min_periods=24).mean().values
        r168 = pd.Series(hit).rolling(168, min_periods=168).mean().values
        for k, t in enumerate(te):
            if (r24[k] < 0.75) or (r168[k] < cb["min_coverage_95"]):
                flags[t] = True
                reasons[t] = f"Coverage drift: 7-day {r168[k]:.0%}, 24 h {r24[k]:.0%}"
        # population stability of monthly net flow vs training months
        nf = self.df["net_flow_usd"]
        train = nf.iloc[:self.fc.idx["cal"][0]]
        edges = np.quantile(train, np.linspace(0, 1, 11))
        edges[0], edges[-1] = -np.inf, np.inf
        base = np.histogram(train, edges)[0] / len(train)
        psi = {}
        month = self.df["timestamp"].dt.to_period("M").astype(str)
        for mth, grp in nf.groupby(month):
            p = np.histogram(grp, edges)[0] / len(grp)
            val = float(np.sum((p - base) * np.log((p + 1e-6) / (base + 1e-6))))
            psi[mth] = val
            if val > cb["max_psi"]:
                ix = grp.index.values
                flags[ix] = True
                for t in ix:
                    reasons[t] = reasons[t] or f"Drift: PSI {val:.2f} in {mth}"
        return flags, reasons, psi

    # ---------- replays ----------
    def replays(self) -> dict:
        key = self._key("full")
        if key not in self._cache:
            runs = {p: self.tr.simulate(p, self.policy, self.balances) for p in POLICIES}
            self._cache[key] = runs
        return self._cache[key]

    def backtest(self, q: float | None = None) -> dict:
        key = self._key("test", q)
        if key not in self._cache:
            out = {}
            init = self.test_init()
            for p in POLICIES:
                out[p] = self.tr.simulate(p, self.policy, self.balances, start=self.test_start, q=q, init=init)
            self._cache[key] = out
        return self._cache[key]

    def test_init(self) -> dict:
        """Every policy starts the test window from the same ledger: the optimized replay on Nov 1."""
        init = state_at(self.opt_run(), self.test_start)
        init["repo_rate"] = float(self.df["tier1_repo_rate_apy"].iloc[self.test_start - 1])
        return init

    def opt_run(self) -> dict:
        return self.replays()["optimized"]

    def state(self, t: int) -> dict:
        s = state_at(self.opt_run(), t + 1)
        s["repo_rate"] = float(self.df["tier1_repo_rate_apy"].iloc[t])
        s["od_limit"] = od_limit(self.policy, self.balances)
        s["wealth"] = s["cash"] + s["repo"] + s["bonds"] + s["equities"] - s["od"]
        return s

    # ---------- views ----------
    def overview(self, t: int) -> dict:
        s = self.state(t)
        R, parts, info = self.tr.reserve(t, self.policy, parts=True)
        headroom = max(0.0, s["od_limit"] - s["od"])
        run = self.tr.runway(t, s["cash"], headroom)
        stale = bool(self.stale_reason) and t == len(self.df) - 1
        on = bool(self.flags[t]) or stale
        status = "breach" if s["cash"] < R * 0.5 else ("watch" if s["cash"] < R or on else "safe")
        hour = int(self.df["hour"].iloc[t])
        nxt = self.next_action(t, s, R)
        opt = self.opt_run()
        lo = max(0, t - 24 * 14)
        rec = opt["rec"]
        terminal = self._terminal(t, s, R, nxt, opt)
        return {
            **terminal,
            "t": t, "ts": self.ts_str[t], "state": s, "reserve": R, "reserve_parts": parts, "reserve_info": info,
            "surplus": s["cash"] - R, "headroom": headroom, "runway": run, "status": status,
            "breaker": {"on": on, "reason": self.stale_reason if stale else self.reasons[t]},
            "next_action": {"title": nxt["title"], "detail": nxt["detail"]}, "hour": hour, "latest": t == len(self.df) - 1,
            "history": {"ts": self.ts_str[lo:t + 1:3],
                        "cash": (rec["cash"][lo:t + 1:3] / 1e6).round(1).tolist(),
                        "reserve": (rec["reserve"][lo:t + 1:3] / 1e6).round(1).tolist(),
                        "repo": (rec["repo"][lo:t + 1:3] / 1e6).round(1).tolist()},
        }

    def _terminal(self, t: int, s: dict, R: float, nxt: dict, run: dict) -> dict:
        """Extra panels for the terminal-style Command Center: today's flows, health, tonight's curve, decisions."""
        df, rec = self.df, run["rec"]
        hour = int(df["hour"].iloc[t])
        day0 = t - hour
        i8 = day0 + 8
        matured = float(rec["repo"][i8 - 1]) if t >= i8 and i8 >= 1 else 0.0
        flows = {"deposits": float(df["deposits_usd"].iloc[day0:t + 1].sum()),
                 "withdrawals": float(df["withdrawals_usd"].iloc[day0:t + 1].sum()),
                 "repo_matured": matured, "sweep_planned": float(nxt.get("sweep", 0.0)),
                 "since": self.ts_str[day0]}
        wealth = max(1.0, s["cash"] + s["repo"] + s["bonds"] + s["equities"])
        cov = self.fc.coverage[1]["static_95"]
        health = {"coverage_ratio": s["cash"] / R if R else None, "band_coverage": cov,
                  "headroom": max(0.0, s["od_limit"] - s["od"]), "breaker": bool(self.flags[t]),
                  "cash_share": s["cash"] / wealth, "bond_share": s["bonds"] / wealth,
                  "psi_max": max(self.psi.values()), "psi_limit": self.policy["circuit_breaker"]["max_psi"],
                  "min_coverage": self.policy["circuit_breaker"]["min_coverage_95"],
                  "breaker_reason": self.reasons[t],
                  "reserve_trend": (rec["reserve"][max(0, t - 47):t + 1:4] / 1e6).round(1).tolist()}
        events = []
        for d in run["decisions"]:
            if t - 72 < d["t"] <= t:
                if d["kind"] == "sweep" and d["sweep"] > 0:
                    events.append({"t": d["t"], "kind": "sweep", "title": f"Swept ${d['sweep']/1e6:,.1f}M into repo",
                                   "detail": f"Kept ${d['night_reserve']/1e6:,.1f}M overnight · {d.get('od_prob', 0):.1%} overdraft chance"})
                elif d["kind"] == "allocation":
                    bought = d["buy_bonds"] + d["buy_equities"]
                    events.append({"t": d["t"], "kind": "allocation",
                                   "title": f"Bought ${bought/1e6:,.1f}M of bonds and equities" if bought > 1e5 else "Held the allocation",
                                   "detail": ", ".join(d["binding"]) or "No binding limits"})
        for i in range(max(1, t - 71), t + 1):
            if df["repo_sweep_mature"].iloc[i] and rec["repo"][i - 1] > 0:
                events.append({"t": i, "kind": "maturity", "title": f"Repo matured: ${rec['repo'][i-1]/1e6:,.1f}M back to cash",
                               "detail": "Overnight carry earned"})
        events.sort(key=lambda e: -e["t"])
        for e in events:
            e["ts"] = self.ts_str[e["t"]]
        return {"flows": flows, "health": health, "events": events[:4], "live": self._live_inputs(t, s),
                "night": {k: nxt[k] for k in ("sweep", "carry", "curve", "s_max", "sweep_ts", "od_prob") if k in nxt}}

    def _live_inputs(self, t: int, s: dict) -> dict:
        """Real inputs for the Command Center's in-browser live model: the next 24 hours of expected flows
        (hour-of-week means before the test months), reserves at each confidence level, and the past week."""
        df, n = self.df, len(self.df)
        how0, hour0 = int(df["how"].iloc[t]), int(df["hour"].iloc[t])
        hows = [(how0 + k) % 168 for k in range(1, 25)]
        hours = [(hour0 + k) % 24 for k in range(1, 25)]
        dep = [round(float(self.prof_dep[h]) / 1e6, 3) for h in hows]
        wd = [round(float(self.prof_wd[h]) / 1e6, 3) for h in hows]
        cfg, r = self.policy, self.policy["reserve"]
        fixed = r["operational_buffer_usd"] + r["min_cash_usd"] + r["committed_payments_usd"]
        qs = [0.90, 0.95, 0.99, 0.999]
        k17 = next((k for k in range(24) if hours[k] == 17), 0)
        cash17 = s["cash"] + sum(dep[:k17 + 1]) * 1e6 - sum(wd[:k17 + 1]) * 1e6
        night = []
        for q in qs:
            c = copy.deepcopy(cfg); c["reserve"]["quantile"] = q
            k_abs = t + k17 + 1
            nl = (self.tr.nightlock(k_abs, cash17, s["od"], s["od_limit"], c, n=600) if k_abs < n else
                  self.tr.nightlock(t, cash17, s["od"], s["od_limit"], c, n=600, how=(how0 + k17 + 1) % 168))
            night.append(nl["night_reserve"])
        day0 = t - hour0
        week = []
        for j in range(7, 0, -1):
            a, b = day0 - 24 * j, day0 - 24 * (j - 1)
            if a < 0:
                continue
            week.append({"day": df["timestamp"].iloc[a].strftime("%a %d"),
                         "dep": round(float(df["deposits_usd"].iloc[a:b].sum()) / 1e6, 1),
                         "wd": round(float(df["withdrawals_usd"].iloc[a:b].sum()) / 1e6, 1)})
        m = self.tr.base_market
        return {"hours": hours, "dep": dep, "wd": wd, "qs": qs,
                "reserve_q": [round(self.tr.reserve(t, cfg, q=q) / 1e6, 2) for q in qs],
                "night_q": [round(x / 1e6, 2) for x in night], "fixed": fixed / 1e6, "night_fixed": r["operational_buffer_usd"] / 1e6,
                "q_default": r["quantile"], "repo_rate": float(m.repo[t]), "bond_yield": float(m.yld[t]),
                "haircuts": [float(x) for x in m.h[t]], "apr": float(m.apr[t]), "week": week,
                "cash": s["cash"] / 1e6, "repo": s["repo"] / 1e6, "bonds": s["bonds"] / 1e6, "equities": s["equities"] / 1e6,
                "od": s["od"] / 1e6, "od_limit": s["od_limit"] / 1e6}

    def next_action(self, t: int, s: dict, R: float) -> dict:
        if self.flags[t]:
            return {"title": "Hold conservative mode", "detail": self.reasons[t]}
        if self.stale_reason and t == len(self.df) - 1:
            return {"title": "Hold conservative mode", "detail": self.stale_reason}
        if s["cash"] < 0:
            lad = self.tr.ladder(t, s, -s["cash"], 6, self.policy)
            return {"title": lad["recommended"]["path"], "detail": f"Cover ${-s['cash']/1e6:,.0f}M gap"}
        # next 17:00 sweep: project cash there with expected flows; the actual sweep is decided at 17:00
        k, how_k, projected = self.next_sweep_hour(t)
        proj = self.project_cash(t, k, s["cash"])
        if projected:
            nl = self.tr.nightlock(t, proj, s["od"], s["od_limit"], self.policy, curve=True, how=how_k)
        else:
            nl = self.tr.nightlock(k, proj, s["od"], s["od_limit"], self.policy, curve=True)
        when = self.df["timestamp"].iloc[t] + pd.Timedelta(hours=k - t)
        return {"title": f"Sweep ≈ ${nl['sweep']/1e6:,.0f}M into repo at 17:00",
                "detail": f"Projected 17:00 cash ${proj/1e6:,.0f}M · keeps ${nl['night_reserve']/1e6:,.0f}M overnight · "
                          f"overdraft probability {nl['od_prob']:.1%}",
                "sweep": nl["sweep"], "carry": nl["carry"], "curve": nl["curve"], "s_max": nl["s_max"],
                "sweep_ts": when.strftime("%Y-%m-%d %H:%M"), "od_prob": nl["od_prob"]}

    def forecast_view(self, h: int, start: int | None) -> dict:
        if start is None:
            start = max(0, int(np.searchsorted(self.fc.bands[h]["test"], self.test_start)))
        s = self.fc.series(h, start=start)
        cov = self.fc.coverage_view(h)
        metrics = [m for m in self.fc.metrics if m["h"] == h]
        return {"h": h, "series": s, "coverage": cov, "metrics": metrics, "all_metrics": self.fc.metrics,
                "q": {str(k): v for k, v in self.fc.bands[h]["q"].items()}, "model_version": MODEL_VERSION,
                "autocorr": self.scen.autocorr_check()}

    def allocation(self, t: int, user: str = "operator") -> dict:
        s = self.state(t)
        lp = self.tr.allocate(t, s, self.policy)
        R, parts, info = self.tr.reserve(t, self.policy, parts=True)
        prev = max(0, t - 24)
        att = self.tr.attribution(prev, t, self.policy)
        lp["current"] = {k: s[k] for k in ("cash", "repo", "bonds", "equities")}
        lp["reserve_parts"] = parts
        lp["attribution"] = att
        lp["attribution_from"] = self.ts_str[prev]
        lp["access"] = [
            {"tier": "Tier 0 cash", "when": "Now", "haircut": 0.0},
            {"tier": "Tier 1 repo", "when": "Next 08:00 (or now at 0.5%)", "haircut": float(self.tr.base_market.h[t][0])},
            {"tier": "Tier 2 bonds", "when": "Same day", "haircut": float(self.tr.base_market.h[t][1])},
            {"tier": "Tier 3 equities", "when": "Same day", "haircut": float(self.tr.base_market.h[t][2])},
            {"tier": "Tier −1 overdraft", "when": "Now, to limit", "haircut": 0.0},
        ]
        lp["decision"] = self.record("allocation", {"t": t}, lp, user)
        return lp

    def ladder(self, t: int, gap: float, duration: float, user: str = "operator") -> dict:
        s = self.state(t)
        out = self.tr.ladder(t, s, gap, duration, self.policy)
        out["state"] = s
        out["decision"] = self.record("ladder", {"t": t, "gap": gap, "duration": duration}, out, user)
        return out

    def nightlock(self, t: int, user: str = "operator") -> dict:
        # move to the 17:00 sweep on or after t (projected when tonight is still ahead, in live mode)
        k, how_k, projected = self.next_sweep_hour(t)
        run = self.opt_run()
        if projected:
            s = self.state(t)
            cash = self.project_cash(t, k, s["cash"])
            out = self.tr.nightlock(t, cash, s["od"], s["od_limit"], self.policy, curve=True, how=how_k,
                                    conservative=bool(self.flags[t] or self.stale_reason))
            out["realized_peak_od"] = None
        else:
            # cash just before the 17:00 sweep: end of 16:00 plus the 17:00 flows
            s = self.state(k - 1)
            cash = s["cash"] + float(self.df["net_flow_usd"].iloc[k])
            out = self.tr.nightlock(k, cash, s["od"], s["od_limit"], self.policy, curve=True,
                                    conservative=bool(self.flags[k]))
            # observed outcome that night in the replay (None while the night is still running)
            i = k - run["start"]
            night = slice(i + 1, min(i + 16, len(run["rec"]["od"])))
            out["realized_peak_od"] = float(run["rec"]["od"][night].max()) if night.stop - night.start >= 15 else None
        out["t"] = min(k, len(self.df) - 1)
        out["projected"] = projected
        out["ts"] = (self.df["timestamp"].iloc[t] + pd.Timedelta(hours=k - t)).strftime("%Y-%m-%d %H:%M")
        sweeps = [d for d in run["decisions"] if d["kind"] == "sweep"]
        out["history"] = self._night_history(sweeps)
        out["decision"] = self.record("nightlock", {"t": k}, out, user)
        return out

    def _night_history(self, sweeps):
        run = self.opt_run()
        od, short = run["rec"]["od"], run["rec"]["short"]
        lim = run["od_limit"]
        pred, obs, pb, ob = [], [], [], []
        for d in sweeps:
            i = d["t"] - run["start"]
            if i + 16 > len(od):
                continue
            pred.append(d["od_prob"])
            pb.append(d["breach_prob"])
            # a night counts only if the overdraft grew after the sweep (not a balance carried in)
            obs.append(bool(od[i + 1:i + 16].max() > od[i] + 1))
            ob.append(bool(od[i + 1:i + 16].max() >= lim - 1 or short[i + 1:i + 16].max() > 0))
        n = len(pred)

        def band(p):
            p = np.array(p); e = float(p.sum()); se = float(np.sqrt((p * (1 - p)).sum())) if n else 0.0
            return e, [max(0.0, e - 1.96 * se), e + 1.96 * se]

        exp, iv = band(pred)
        bexp, biv = band(pb)
        return {"nights": n, "expected_od_nights": exp, "observed_od_nights": int(np.sum(obs)), "interval": iv,
                "expected_breach_nights": bexp, "observed_breach_nights": int(np.sum(ob)), "breach_interval": biv,
                "avg_sweep": float(np.mean([d["sweep"] for d in sweeps])) if n else 0.0}

    def price_of_safety(self) -> dict:
        key = self._key("pos")
        if key not in self._cache:
            pts = []
            for q in (0.90, 0.95, 0.975, 0.99, 0.995, 0.999):
                mt = self.tr.simulate("optimized", self.policy, self.balances, start=self.test_start, q=q,
                                      init=self.test_init())["metrics"]
                pts.append({"q": q, "shortfall_hours": mt["shortfall_hours"], "net_return": mt["net_return"],
                            "overdraft_cost": mt["overdraft_cost"], "avg_cash": mt["avg_cash"],
                            "repo_income": mt["income"]["repo"]})
            for a, b in zip(pts, pts[1:]):
                b["cost_vs_prev"] = a["net_return"] - b["net_return"]
            self._cache[key] = pts
        return self._cache[key]

    def backtest_view(self) -> dict:
        bt = self.backtest()
        ms = [bt[p]["metrics"] for p in POLICIES]
        # capture = share of the improvement over doing nothing that perfect hindsight achieves
        base = bt["conservative"]["metrics"]["net_return"]
        room = bt["oracle"]["metrics"]["net_return"] - base
        opt_reserve = bt["optimized"]["rec"]["reserve"]
        for m, p in zip(ms, POLICIES):
            m["capture"] = ((m["net_return"] - base) / room) if room > 0 else None
            m["idle_cash"] = float(np.maximum(0, bt[p]["rec"]["cash"] - opt_reserve).mean())
        return {"window": [self.ts_str[self.test_start], self.ts_str[-1]], "metrics": ms,
                "oracle_trades": self.tr.oracle_plan(self.policy, self.balances, self.test_start, len(self.df),
                                                     self.tr.base_market, self.test_init())["trades"],
                "start_state": self.test_init(),
                "price_of_safety": self.price_of_safety()}

    def replay_view(self, step: int = 1) -> dict:
        runs = self.replays()
        out = {"ts": self.ts_str[::step], "policies": {}}
        for p in ("fixed", "optimized"):
            r = runs[p]["rec"]
            out["policies"][p] = {k: (r[k][::step] / 1e6).round(1).tolist() for k in ("cash", "repo", "bonds", "equities", "od")}
            out["policies"][p]["short"] = (r["short"][::step] / 1e6).round(1).tolist()
        out["reserve"] = (runs["optimized"]["rec"]["reserve"][::step] / 1e6).round(1).tolist()
        out["breaker"] = runs["optimized"]["breaker"][::step].astype(int).tolist()
        out["alerts"] = runs["optimized"]["alerts"][:400]
        out["metrics"] = {p: runs[p]["metrics"] for p in POLICIES}
        return out

    def twin(self, shocks: dict, user: str = "analyst") -> dict:
        out = twin(self.tr, self.df, self.policy, self.balances, self.opt_run(), shocks)
        out["decision"] = self.record("scenario", shocks, out["metrics"], user)
        return out

    def reverse(self, x_axis: str, y_axis: str, start: int | None, user: str = "analyst") -> dict:
        key = self._key("rev", x_axis, y_axis, start)
        if key not in self._cache:
            self._cache[key] = reverse_stress(self.tr, self.df, self.policy, self.balances, self.opt_run(),
                                              x_axis, y_axis, start)
        out = self._cache[key]
        self.record("reverse_stress", {"x": x_axis, "y": y_axis, "start": out["start"]},
                    {"margin": out["margin"]}, user)
        return out

    def drift_test(self, factor: float) -> dict:
        return self.fc.drift_test(1, factor)

    def trust(self) -> dict:
        return {"meta": self.meta, "honesty": self.honesty, "psi": self.psi,
                "policy": self.policy_rec, "versions": self.store.policy_versions(),
                "approvals": self.store.approvals(), "audit": self.store.entries(),
                "verify": self.store.verify(), "wrongway": self.wrongway, "boot_seconds": self.boot_seconds,
                "balances": self.balances, "model_version": MODEL_VERSION, "dataset": self.dataset,
                "splits": {"cal": self.fc.bounds["cal"], "test": self.fc.bounds["test"], "models_cached": self.fc.cached}}

    # ---------- decision records (FR-12) ----------
    def record(self, kind: str, inputs: dict, output: dict, user: str) -> dict:
        digest = hashlib.sha256(canonical(_strip(output)).encode()).hexdigest()
        payload = {"kind": kind, "inputs": inputs, "dataset_sha": self.meta["sha256"][:16],
                   "dataset": self.dataset["id"], "rows": len(self.df),
                   "policy_version": self.policy_rec["version"], "model_version": MODEL_VERSION,
                   "seed": self.scen.seed, "balances": self.balances, "output_sha": digest, "by": user}
        e = self.store.log("decision", payload)
        return {"id": e["seq"], "output_sha": digest[:12]}

    def reproduce(self, seq: int) -> dict:
        rec = self.store.get_entry(seq)
        if not rec or rec["kind"] != "decision":
            raise ValueError("Not a decision record")
        p = rec["payload"]
        if p.get("dataset", "demo") != self.dataset["id"]:
            return {"id": seq, "match": None, "note": "This decision was made on a different dataset"}
        if p.get("dataset_sha") != self.meta["sha256"][:16]:
            return {"id": seq, "match": None, "note": "The dataset has new rows since this decision (live data)"}
        if p["policy_version"] != self.policy_rec["version"] or p["balances"] != self.balances:
            return {"id": seq, "match": None, "note": "Policy or balances changed since this decision"}
        i = p["inputs"]
        kind = p["kind"]
        if kind == "allocation":
            s = self.state(i["t"]); out = self.tr.allocate(i["t"], s, self.policy)
            out["current"] = {k: s[k] for k in ("cash", "repo", "bonds", "equities")}
            R, parts, _ = self.tr.reserve(i["t"], self.policy, parts=True)
            out["reserve_parts"] = parts
            att = self.tr.attribution(max(0, i["t"] - 24), i["t"], self.policy)
            out["attribution"] = att; out["attribution_from"] = self.ts_str[max(0, i["t"] - 24)]
            out["access"] = None
        elif kind == "ladder":
            s = self.state(i["t"]); out = self.tr.ladder(i["t"], s, i["gap"], i["duration"], self.policy); out["state"] = s
        else:
            return {"id": seq, "match": None, "note": f"Reproduction not wired for {kind}"}
        digest = hashlib.sha256(canonical(_strip(out)).encode()).hexdigest()
        return {"id": seq, "match": digest == p["output_sha"], "expected": p["output_sha"][:12], "got": digest[:12]}


def _strip(o):
    """Drop volatile keys before hashing a decision output."""
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in ("decision", "access", "history", "curve")}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return round(float(o), 4)
    if isinstance(o, np.integer):
        return int(o)
    return o
