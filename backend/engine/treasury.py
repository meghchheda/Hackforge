"""Treasury logic: reserve (FR-04), ledger (FR-05), LP optimizer (FR-06), repo timing (FR-07),
liquidation model (FR-08), funding ladder (FR-14), runway (FR-15), night-lock (FR-16),
policy replay (FR-13), perfect-hindsight benchmark (FR-21) and reserve attribution (FR-22)."""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from .scenarios import ScenarioEngine

DEFAULT_POLICY = {
    "version": 1,
    "reserve": {"quantile": 0.99, "min_cash_usd": 10_000_000, "operational_buffer_usd": 10_000_000,
                "committed_payments_usd": 0, "count_overdraft": True},
    "caps": {"tier2_bonds": 0.30, "tier3_equities": 0.10},
    "overdraft": {"limit_usd": "auto", "limit_share_of_wealth": 0.10},
    "nightlock": {"max_breach_probability": 0.001},
    "circuit_breaker": {"stale_after_hours": 2, "min_coverage_95": 0.93, "max_psi": 0.25},
    "approvals": {"maker_checker_above_usd": 25_000_000},
    "assumptions": {"equity_expected_return": 0.07, "risk_aversion": 3.0, "lp_horizon_days": 30, "equity_stress": 0.20,
                    "bond_stress_bp": 100},
    "baseline": {"fixed_cash_floor_usd": 200_000_000},
}
DEFAULT_BALANCES = {"cash": 2_400_000_000, "bonds": 1_200_000_000, "equities": 400_000_000}
POLICIES = ("conservative", "fixed", "optimized", "oracle")
POLICY_LABELS = {"conservative": "Conservative (all cash)", "fixed": "Fixed reserve",
                 "optimized": "Optimized", "oracle": "Perfect hindsight"}


def od_limit(cfg: dict, balances: dict) -> float:
    lim = cfg["overdraft"]["limit_usd"]
    if lim == "auto":
        return cfg["overdraft"]["limit_share_of_wealth"] * sum(balances.values())
    return float(lim)


def hours_to_maturity(hour: int) -> int:
    d = (8 - hour) % 24
    return d if d else 24


class Market:
    """Arrays of observed data, optionally shocked for Digital Twin runs (FR-11)."""

    def __init__(self, df: pd.DataFrame, shocks: dict | None = None):
        self.ts = df["timestamp"]
        self.dep = df["deposits_usd"].values.astype(float).copy()
        self.wd = df["withdrawals_usd"].values.astype(float).copy()
        self.repo = df["tier1_repo_rate_apy"].values.astype(float).copy()
        self.yld = df["tier2_bond_yield_pct"].values.astype(float).copy()
        self.bpx = df["tier2_bond_price_index"].values.astype(float).copy()
        self.eq = df["tier3_stock_index_price"].values.astype(float).copy()
        self.dur = float(df["tier2_bond_mod_duration"].iloc[0])
        self.vol = float(df["tier3_stock_volatility_pct"].iloc[0])
        self.h = np.c_[df["tier1_early_redemption_haircut_pct"].values,
                       df["tier2_bond_liquidation_haircut_pct"].values,
                       df["tier3_stock_liquidation_haircut_pct"].values].astype(float) / 100
        self.apr = df["emergency_overdraft_penalty_apr_pct"].values.astype(float) / 100
        self.hour = df["hour"].values
        self.how = df["how"].values
        self.sweep = df["repo_sweep_window_open"].values
        self.mature = df["repo_sweep_mature"].values
        self.stale = np.zeros(len(df), bool)
        self.missed = set()
        if shocks:
            self._apply(shocks)

    def _apply(self, s: dict):
        a = int(s.get("start", 0))
        b = min(a + int(s.get("hours", 72)), len(self.dep))
        self.wd[a:b] *= float(s.get("withdrawal_mult", 1.0))
        delay = int(s.get("deposit_delay_h", 0))
        if delay > 0:
            held = self.dep[a:min(a + delay, b)].sum()
            self.dep[a:min(a + delay, b)] = 0
            self.dep[min(a + delay, len(self.dep) - 1)] += held
        drop = float(s.get("equity_drop", 0.0))
        if drop:
            self.eq[a:] *= (1 - drop)
        bp = float(s.get("yield_shock_bp", 0.0))
        if bp:
            self.bpx[a:] *= (1 - self.dur * bp / 10_000)
            self.yld[a:] += bp / 100
        add = float(s.get("haircut_add_pct", 0.0))
        if add:
            self.h[a:b] += add / 100
        if s.get("missed_maturity"):
            nxt = next((t for t in range(a, b) if self.mature[t]), None)
            if nxt is not None:
                self.missed.add(nxt)
        if s.get("stale_feed"):
            self.stale[a:b] = True


class Treasury:
    def __init__(self, df: pd.DataFrame, scen: ScenarioEngine, breaker_flags: np.ndarray,
                 breaker_reasons: list, train_end: int | None = None):
        self.df = df
        self.scen = scen
        self.base_market = Market(df)
        self.n = len(df)
        self.breaker_flags = breaker_flags
        self.breaker_reasons = breaker_reasons
        nf = df["net_flow_usd"].values
        train_end = train_end or int(len(nf) * 2 / 3)
        win = min(720, max(24, train_end // 2))
        roll = pd.Series(nf[:train_end]).rolling(win).sum().dropna()
        self.need_30d = float(max(0.0, -roll.quantile(0.05))) if len(roll) else 0.0
        self.sigma = float(np.std(scen.pool))
        # expected cumulative net flow over the next k hours from each hour-of-week (drift included)
        prof = np.tile(scen.profile, 3)
        self.prof_cum = np.array([np.cumsum(prof[h + 1:h + 1 + 168]) for h in range(168)])

    def expected_gap_hours(self, how: int, need: float, repo: float, to_mat: int) -> float:
        """Hours until expected inflows (plus maturing repo) cover a gap; inf if not within a week."""
        cum = self.prof_cum[how].copy()
        if repo > 0:
            cum[to_mat - 1:] += repo
        k = np.where(cum >= need)[0]
        return float(k[0] + 1) if len(k) else float("inf")

    # ---------- reserve (FR-04) ----------
    def _resid(self, t: int) -> float:
        """Current flow surprise; zero for projected hours beyond the observed data."""
        return float(self.scen.resid_all[t]) if 0 <= t < len(self.scen.resid_all) else 0.0

    def reserve(self, t: int, cfg: dict, q: float | None = None, parts: bool = False, how: int | None = None):
        r = cfg["reserve"]
        q = q or r["quantile"]
        if how is None:
            how = int(self.base_market.how[min(t, self.n - 1)])
        H = hours_to_maturity(int(how) % 24) + 24
        e0 = self._resid(t)
        med = self.scen.peak_quantile(how, H, 0.5)
        peak = self.scen.peak_quantile(how, H, q)
        mom = self.scen.momentum(e0, H)
        stressed = max(0.0, peak + mom)
        comp = {
            "expected_peak_outflow": med,                 # median path
            "uncertainty_addon": peak - med,              # chosen quantile above the median
            "momentum": stressed - peak,                  # persistence of the current flow surprise
            "committed_payments": float(r["committed_payments_usd"]),
            "operational_buffer": float(r["operational_buffer_usd"]),
            "minimum_cash": float(r["min_cash_usd"]),
        }
        total = sum(comp.values())
        if parts:
            return total, comp, {"horizon_h": H, "quantile": q, "residual_now": e0}
        return total

    # ---------- runway clock (FR-15) ----------
    def runway(self, t: int, cash: float, headroom: float, length: int = 72) -> dict:
        fan = self.scen.fan(t, length=length, n=1000)
        cum = fan.pop("cum_paths")
        avail = cash + headroom
        hit = (avail + cum) < 0
        first = np.where(hit.any(axis=1), hit.argmax(axis=1) + 1, length + 1)
        out = {}
        for name, q in (("p50", 0.5), ("p95", 0.05), ("p99", 0.01)):
            v = float(np.quantile(first, q))
            out[name] = None if v > length else int(v)
        return {"hours": out, "horizon": length, "fan": fan, "available": avail}

    # ---------- funding ladder (FR-14) ----------
    def ladder(self, t: int, state: dict, gap: float, duration_h: float, cfg: dict,
               market: Market | None = None) -> dict:
        m = market or self.base_market
        apr = float(m.apr[t])
        h1, h2, h3 = (float(x) for x in m.h[t])
        hourly = apr / 8760
        headroom = max(0.0, state["od_limit"] - state["od"])
        to_mat = hours_to_maturity(int(m.hour[t]))
        paths = []

        def add(name, cost, access, feasible, note):
            paths.append({"path": name, "cost": float(cost), "cost_per_dollar": float(cost / gap) if gap else 0,
                          "access": access, "feasible": bool(feasible), "note": note})

        add("Borrow on overdraft for the whole gap", gap * hourly * duration_h, "Immediate",
            gap <= headroom, f"{duration_h:.0f} h at {apr:.0%} APR")
        if state["repo"] > 0 and to_mat < duration_h:
            add("Borrow until 08:00, repay from maturing repo", gap * hourly * to_mat, "Immediate",
                gap <= headroom and state["repo"] >= gap, f"Bridge {to_mat} h, repo matures 08:00")
        add("Redeem repo early", gap * h1 / (1 - h1), "Immediate",
            state["repo"] * (1 - h1) >= gap, f"{h1:.1%} haircut")
        add("Sell bonds", gap * h2 / (1 - h2), "Same day",
            state["bonds"] * (1 - h2) >= gap, f"{h2:.0%} haircut + price risk")
        add("Sell equities", gap * h3 / (1 - h3), "Same day",
            state["equities"] * (1 - h3) >= gap, f"{h3:.0%} haircut + price risk")
        if gap > headroom > 0:
            rest = gap - headroom
            best = min([(h1, "repo", state["repo"]), (h2, "bonds", state["bonds"]),
                        (h3, "equities", state["equities"])], key=lambda x: x[0] if x[2] * (1 - x[0]) >= rest else 9)
            add(f"Overdraft to limit, then {best[1]}", headroom * hourly * duration_h + rest * best[0] / (1 - best[0]),
                "Immediate", best[2] * (1 - best[0]) >= rest, f"${headroom/1e6:,.0f}M borrowed + ${rest/1e6:,.0f}M sold")
        ranked = sorted(paths, key=lambda p: (not p["feasible"], p["cost"]))
        breakeven = {"repo": h1 / hourly, "bonds": h2 / hourly, "equities": h3 / hourly}
        return {"gap": gap, "duration_h": duration_h, "headroom": headroom, "hours_to_maturity": to_mat,
                "paths": ranked, "recommended": ranked[0], "breakeven_hours": breakeven, "apr": apr}

    # ---------- night-lock (FR-16) ----------
    def nightlock(self, t: int, cash: float, od: float, odlim: float, cfg: dict, n: int = 1000,
                  market: Market | None = None, conservative: bool = False, curve: bool = False,
                  how: int | None = None) -> dict:
        """Size the 17:00 sweep at hour t. `how` projects to a later 17:00 (live mode: tonight has not happened yet),
        using today's rates and no flow surprise."""
        m = market or self.base_market
        e0 = self._resid(t) if how is None else 0.0
        how = int(m.how[t]) if how is None else int(how)
        L = 15
        paths = self.scen.paths(how, L, n, e0=e0, seed=self.scen.seed + 7 * t)
        if market is not None:
            shocked = m.dep[t + 1:t + 1 + L] - m.wd[t + 1:t + 1 + L]
            base = self.base_market.dep[t + 1:t + 1 + L] - self.base_market.wd[t + 1:t + 1 + L]
            if len(shocked) == L and len(base) == L:
                paths = paths + (shocked - base)  # carry Digital Twin shocks into the overnight paths
        cum = np.cumsum(paths, axis=1)
        low = cum.min(axis=1)
        peak = np.maximum(0, -low)
        r = cfg["reserve"]
        q = 0.999 if conservative else r["quantile"]
        buffer = r["operational_buffer_usd"] * (2 if conservative else 1) + r["committed_payments_usd"]
        night_reserve = max(r["min_cash_usd"], float(np.quantile(peak, q)) + buffer)
        s_max = max(0.0, cash - night_reserve)
        rate = float(m.repo[t]) / 100
        apr = float(m.apr[t])
        headroom = max(0.0, odlim - od)
        grid = np.linspace(0, max(cash, 1.0), 81)
        carry = grid * rate * L / 8760
        short = np.maximum(0, grid[:, None, None] - cash - cum[None, :, :])      # S × paths × hours
        exp_cost = short.mean(axis=1).sum(axis=1) * apr / 8760
        breach = ((cash - grid[:, None] + low[None, :] + headroom) < 0).mean(axis=1)
        od_prob = ((cash - grid[:, None] + low[None, :]) < 0).mean(axis=1)
        net = carry - exp_cost
        cap = cfg["nightlock"]["max_breach_probability"]
        ok = (grid <= s_max + 1) & (breach <= cap)
        best = int(np.argmax(np.where(ok, net, -np.inf))) if ok.any() else 0
        s_star = float(grid[best]) if ok.any() else 0.0
        out = {"sweep": s_star, "cash": cash, "night_reserve": night_reserve, "breach_prob": float(breach[best]),
               "od_prob": float(od_prob[best]),
               "carry": float(carry[best]), "expected_od_cost": float(exp_cost[best]), "rate": rate,
               "s_max": s_max, "cap": cap}
        if curve:
            out["curve"] = {"sweep": (grid / 1e6).round(1).tolist(), "carry": carry.round(0).tolist(),
                            "exp_cost": exp_cost.round(0).tolist(), "net": net.round(0).tolist(),
                            "breach": breach.round(5).tolist(), "od_prob": od_prob.round(4).tolist()}
        return out

    # ---------- allocation LP (FR-06) ----------
    def allocate(self, t: int, state: dict, cfg: dict, market: Market | None = None) -> dict:
        m = market or self.base_market
        a = cfg["assumptions"]
        T = a["lp_horizon_days"]
        W = state["cash"] + state["repo"] + state["bonds"] + state["equities"]
        h1, h2, h3 = (float(x) for x in m.h[t])
        R = self.reserve(t, cfg, how=int(m.how[t]))
        headroom = max(0.0, state["od_limit"] - state["od"])
        how = int(m.how[t])
        q = cfg["reserve"]["quantile"]
        s24 = self.scen.peak_quantile(how, 24, q)
        s72 = self.scen.peak_quantile(how, 72, q)
        bond_stress = m.dur * a["bond_stress_bp"] / 10_000
        eq_stress = a["equity_stress"]
        r1 = float(m.repo[t]) / 100 * (15 / 24) * T / 365
        r2 = float(m.yld[t]) / 100 * T / 365
        # risk-adjusted equity return: mean − ½·γ·σ², using the supplied 18% volatility
        sigma = m.vol / 100
        r3 = (a["equity_expected_return"] - 0.5 * a.get("risk_aversion", 0.0) * sigma ** 2) * T / 365
        b0, e0 = state["bonds"], state["equities"]
        cap2 = max(cfg["caps"]["tier2_bonds"] * W, b0)
        cap3 = max(cfg["caps"]["tier3_equities"] * W, e0)
        # variables: x0 cash, x1 repo, x2 bonds, x3 equities, u2, s2, u3, s3, d0..d1 turnover
        c = -np.array([0, r1, r2, r3, 0, -h2, 0, -h3])
        A_eq = [[1, 1, 1, 1, 0, h2, 0, h3],
                [0, 0, 1, 0, -1, 1, 0, 0],
                [0, 0, 0, 1, 0, 0, -1, 1]]
        b_eq = [W, b0, e0]
        A_ub, b_ub, names = [], [], []
        # undrawn overdraft counts toward the reserve: borrowing beats selling for short gaps (PRD §5)
        od_credit = headroom if cfg["reserve"].get("count_overdraft", True) else 0.0
        A_ub.append([-1, 0, 0, 0, 0, 0, 0, 0]); b_ub.append(-(R - od_credit))
        names.append("Reserve: cash + undrawn overdraft ≥ reserve")
        floor = cfg["reserve"]["min_cash_usd"] + cfg["reserve"]["operational_buffer_usd"]
        A_ub.append([-1, 0, 0, 0, 0, 0, 0, 0]); b_ub.append(-floor); names.append("Operating cash floor")
        A_ub.append([-1, -1, -(1 - h2) * (1 - bond_stress), -(1 - h3) * (1 - eq_stress), 0, 0, 0, 0])
        b_ub.append(-(s24 - headroom)); names.append("24 h stressed coverage")
        A_ub.append([-1, -1, -(1 - h2) * (1 - bond_stress), -(1 - h3) * (1 - eq_stress), 0, 0, 0, 0])
        b_ub.append(-(s72 - headroom)); names.append("72 h stressed coverage")
        A_ub.append([0, 0, 1, 0, 0, 0, 0, 0]); b_ub.append(cap2); names.append("Bond cap")
        A_ub.append([0, 0, 0, 1, 0, 0, 0, 0]); b_ub.append(cap3); names.append("Equity cap")
        res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=[(0, None)] * 8, method="highs")
        if res.status != 0:
            return {"feasible": False, "reserve": R, "message": res.message, "W": W}
        x = res.x
        slack = np.array(b_ub) - np.array(A_ub) @ x
        binding = [names[i] for i in range(len(names)) if abs(slack[i]) < 1e-3 * max(1.0, abs(b_ub[i]))]
        target = {"cash": x[0], "repo": x[1], "bonds": x[2], "equities": x[3]}
        exp_ret = {"repo": r1 * x[1], "bonds": r2 * x[2], "equities": r3 * x[3]}
        return {"feasible": True, "W": W, "reserve": R, "target": target, "buy_bonds": x[4], "sell_bonds": x[5],
                "buy_equities": x[6], "sell_equities": x[7], "binding": binding,
                "expected_return_horizon": float(sum(exp_ret.values()) - h2 * x[5] - h3 * x[7]),
                "returns": {"repo": r1, "bonds": r2, "equities": r3}, "horizon_days": T,
                "coverage_need": {"24h": s24, "72h": s72}, "headroom": headroom}

    # ---------- ledger simulator + policies (FR-05, FR-13) ----------
    def simulate(self, policy: str, cfg: dict, balances: dict, start: int = 0, end: int | None = None,
                 market: Market | None = None, init: dict | None = None, fast: bool = False,
                 q: float | None = None, oracle_plan: dict | None = None, breaker: bool = True) -> dict:
        m = market or self.base_market
        n = len(m.dep)
        end = n if end is None else min(end, n)
        if q is not None:
            cfg = copy.deepcopy(cfg); cfg["reserve"]["quantile"] = q
        odlim = od_limit(cfg, balances)
        if init:
            cash, repo, bonds, eq, od = (init[k] for k in ("cash", "repo", "bonds", "equities", "od"))
            repo_rate = init.get("repo_rate", float(m.repo[start]))
        else:
            cash, repo, bonds, eq, od = balances["cash"], 0.0, balances["bonds"], balances["equities"], 0.0
            repo_rate = 0.0
        if policy == "oracle" and oracle_plan is None:
            oracle_plan = self.oracle_plan(cfg, balances, start, end, m, init)
        if policy == "oracle":
            cash += oracle_plan["cash_delta"]; bonds = oracle_plan["bonds"]; eq = oracle_plan["equities"]
        inc = {"repo": 0.0, "bond_accrual": 0.0, "bond_price": 0.0, "equity": 0.0, "haircuts": 0.0,
               "overdraft": 0.0}
        if policy == "oracle":
            inc["haircuts"] += oracle_plan["haircut_cost"]
        L = end - start
        rec = {k: np.zeros(L) for k in ("cash", "repo", "bonds", "equities", "od", "reserve", "short")}
        brk = np.zeros(L, bool)
        alerts, decisions = [], []
        short_hours, max_short, total_short, forced = 0, 0.0, 0.0, 0.0
        pending_mature = False
        for i, t in enumerate(range(start, end)):
            hour = int(m.hour[t])
            # 1. repo maturity at 08:00
            if m.mature[t] and repo > 0:
                if t in m.missed:
                    pending_mature = True
                    if policy == "optimized" and not fast:
                        alerts.append(self._alert(t, "critical", "Missed repo maturity", "Repo did not return at 08:00"))
                else:
                    gain = repo * repo_rate / 100 * (15 + (24 if pending_mature else 0)) / 8760
                    cash += repo + gain; inc["repo"] += gain; repo = 0.0; pending_mature = False
            # 2. market moves and accruals
            if t > 0:
                pb = bonds * (m.bpx[t] / m.bpx[t - 1] - 1); acc = bonds * m.yld[t] / 100 / 8760
                bonds += pb + acc; inc["bond_price"] += pb; inc["bond_accrual"] += acc
                pe = eq * (m.eq[t] / m.eq[t - 1] - 1); eq += pe; inc["equity"] += pe
            if od > 0:
                oi = od * m.apr[t] / 8760; od += oi; inc["overdraft"] += oi
            # 3. flows
            cash += m.dep[t] - m.wd[t]
            # breaker state
            on = False
            if breaker and policy == "optimized":
                on = bool((t < len(self.breaker_flags) and self.breaker_flags[t]) or m.stale[t])
            brk[i] = on
            # 4. fund any gap
            if cash < 0:
                need = -cash
                if policy in ("optimized", "oracle"):
                    # funding ladder: borrow only while the expected gap is shorter than the break-even
                    d = self.expected_gap_hours(int(m.how[t]), need, repo, hours_to_maturity(hour))
                    h1, h2 = float(m.h[t][0]), float(m.h[t][1])
                    borrow = need * m.apr[t] * d / 8760
                    sell = need * (h1 / (1 - h1) if repo > 0 else h2 / (1 - h2))
                    order = ("od", "repo", "bonds", "equities") if borrow <= sell \
                        else ("repo", "bonds", "od", "equities")
                else:
                    order = ("repo", "bonds", "equities", "od")
                for src in order:
                    if need <= 0:
                        break
                    if src == "od":
                        take = min(need, max(0.0, odlim - od)); od += take; cash += take; need -= take
                    else:
                        hc = float(m.h[t][("repo", "bonds", "equities").index(src)])
                        bal = {"repo": repo, "bonds": bonds, "equities": eq}[src]
                        face = min(bal, need / (1 - hc))
                        got = face * (1 - hc)
                        inc["haircuts"] += face - got; cash += got; need -= got
                        if src != "repo":
                            forced += got
                        if src == "repo": repo -= face
                        elif src == "bonds": bonds -= face
                        else: eq -= face
                if cash < -1:
                    short_hours += 1; total_short += -cash; max_short = max(max_short, -cash)
                    if policy == "optimized" and not fast:
                        alerts.append(self._alert(t, "critical", "Payout shortfall", f"Unfunded gap ${-cash/1e6:,.1f}M"))
            # 5. repay overdraft
            floor = cfg["reserve"]["min_cash_usd"]
            if od > 0 and cash > floor:
                pay = min(od, cash - floor); od -= pay; cash -= pay
            # 6. decisions
            R = self.reserve(t, cfg, how=int(m.how[t])) if policy == "optimized" or not fast else 0.0
            if policy == "optimized" and hour == 9 and not on and not fast:
                st = {"cash": cash, "repo": repo, "bonds": bonds, "equities": eq, "od": od, "od_limit": odlim}
                lp = self.allocate(t, st, cfg, m)
                if lp["feasible"]:
                    buy_b = min(lp["buy_bonds"], max(0.0, cash - R)) if lp["buy_bonds"] > 5e6 else 0.0
                    cash -= buy_b; bonds += buy_b
                    buy_e = min(lp["buy_equities"], max(0.0, cash - R)) if lp["buy_equities"] > 5e6 else 0.0
                    cash -= buy_e; eq += buy_e
                    for src, amt, k in (("bonds", lp["sell_bonds"], 1), ("equities", lp["sell_equities"], 2)):
                        if amt > 5e6:
                            hc = float(m.h[t][k]); inc["haircuts"] += amt * hc; cash += amt * (1 - hc)
                            if src == "bonds": bonds -= amt
                            else: eq -= amt
                    decisions.append({"t": t, "kind": "allocation", "binding": lp["binding"], "reserve": R,
                                      "buy_bonds": buy_b, "buy_equities": buy_e})
                else:
                    on = True; brk[i] = True
                    alerts.append(self._alert(t, "warning", "Optimizer infeasible", "Fell back to conservative policy"))
            if m.sweep[t] and cash > 0:
                if policy == "fixed":
                    s = max(0.0, cash - cfg["baseline"]["fixed_cash_floor_usd"])
                elif policy == "optimized":
                    nl = self.nightlock(t, cash, od, odlim, cfg, n=300 if fast else 800,
                                        market=market, conservative=on)
                    s = nl["sweep"]
                    if not fast:
                        decisions.append({"t": t, "kind": "sweep", "sweep": s, "breach_prob": nl["breach_prob"],
                                          "od_prob": nl["od_prob"],
                                          "night_reserve": nl["night_reserve"]})
                elif policy == "oracle":
                    s = oracle_plan["sweeps"].get(t, 0.0)
                    s = max(0.0, min(s, cash))
                else:
                    s = 0.0
                if s > 0:
                    repo += s; cash -= s; repo_rate = float(m.repo[t])
            # alerts (shock radar FR-09)
            if policy == "optimized" and not fast:
                e = self._resid(t) if market is None else (m.dep[t] - m.wd[t]) - self.scen.profile[m.how[t]]
                if e < -4 * self.sigma:
                    alerts.append(self._alert(t, "warning", "Outflow spike", f"Net flow {e/self.sigma:+.1f}σ vs hour-of-week norm"))
                if on and (i == 0 or not brk[i - 1]):
                    alerts.append(self._alert(t, "warning", "Circuit breaker tripped",
                                              "Stale feed" if m.stale[t] else
                                              (self.breaker_reasons[t] if t < len(self.breaker_reasons) else "") or "Model check failed"))
                if od > 0 and (i == 0 or rec["od"][i - 1] == 0):
                    alerts.append(self._alert(t, "info", "Overdraft drawn", f"${od/1e6:,.1f}M at {m.apr[t]:.0%} APR"))
            rec["cash"][i], rec["repo"][i], rec["bonds"][i], rec["equities"][i], rec["od"][i] = cash, repo, bonds, eq, od
            rec["reserve"][i] = R
            rec["short"][i] = -cash if cash < -1 else 0
        end_w = cash + repo + bonds + eq - od
        start_w = sum(balances.values()) if not init else init["cash"] + init["repo"] + init["bonds"] + init["equities"] - init["od"]
        flows = float((m.dep[start:end] - m.wd[start:end]).sum())
        net_return = sum(v for k, v in inc.items() if k not in ("haircuts", "overdraft")) - inc["haircuts"] - inc["overdraft"]
        reserve_ref = rec["reserve"] if policy == "optimized" else None
        metrics = {
            "policy": policy, "label": POLICY_LABELS[policy],
            "shortfall_hours": short_hours, "max_shortfall": max_short, "total_shortfall": total_short,
            "net_return": float(net_return), "income": {k: float(v) for k, v in inc.items()},
            "overdraft_cost": inc["overdraft"], "haircut_cost": inc["haircuts"],
            "avg_cash": float(rec["cash"].mean()),
            "start_wealth": start_w, "end_wealth": end_w, "flows": flows,
            "breaker_hours": int(brk.sum()),
            "forced_sales": float(forced),
            "min_liquid": float((rec["cash"] + rec["repo"]).min()) if L else 0.0,
            "peak_od": float(rec["od"].max()) if L else 0.0,
            "invariant_error": float(abs(end_w - (start_w + flows + net_return))),
        }
        # collapse repeats: one alert per title per 6 hours
        kept, last = [], {}
        for a in alerts:
            if a["t"] - last.get(a["title"], -99) >= 6:
                kept.append(a)
            last[a["title"]] = a["t"]
        alerts = kept
        return {"metrics": metrics, "rec": rec, "breaker": brk, "alerts": alerts, "decisions": decisions,
                "start": start, "end": end, "od_limit": odlim, "reserve_ref": reserve_ref,
                "final": {"cash": cash, "repo": repo, "bonds": bonds, "equities": eq, "od": od, "repo_rate": repo_rate}}

    @staticmethod
    def _alert(t, level, title, detail):
        return {"t": int(t), "level": level, "title": title, "detail": detail}

    # ---------- perfect hindsight (FR-21) ----------
    def oracle_plan(self, cfg: dict, balances: dict, start: int, end: int, m: Market, init: dict | None) -> dict:
        cash0 = init["cash"] if init else balances["cash"]
        b0 = init["bonds"] if init else balances["bonds"]
        e0 = init["equities"] if init else balances["equities"]
        W = cash0 + b0 + e0
        flows = m.dep[start:end] - m.wd[start:end]
        cum = np.cumsum(flows)
        need = max(0.0, -cum.min()) + cfg["reserve"]["min_cash_usd"]
        G2 = (m.bpx[end - 1] / m.bpx[start]) - 1 + float(m.yld[start:end].mean()) / 100 * (end - start) / 8760
        G3 = m.eq[end - 1] / m.eq[start] - 1
        nights = int(m.sweep[start:end].sum())
        g1 = float(m.repo[start:end].mean()) / 100 * 15 / 8760 * nights * 0.9
        h1, h2, h3 = (float(x) for x in m.h[start])
        cap2 = max(cfg["caps"]["tier2_bonds"] * W, b0); cap3 = max(cfg["caps"]["tier3_equities"] * W, e0)
        # vars: u2, s2, u3, s3 ; liquid L = cash0 - u2 - u3 + (1-h2)s2 + (1-h3)s3
        c = -np.array([G2 - g1, -(G2 - g1) - h2 * (1 + g1), G3 - g1, -(G3 - g1) - h3 * (1 + g1)])
        A_ub = [[1, -(1 - h2), 1, -(1 - h3)],     # liquidity: L ≥ need
                [1, -1, 0, 0], [0, 0, 1, -1]]      # caps
        b_ub = [cash0 - need, cap2 - b0, cap3 - e0]
        bounds = [(0, None), (0, b0), (0, None), (0, e0)]
        res = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
        u2, s2, u3, s3 = res.x if res.status == 0 else (0, 0, 0, 0)
        sweeps = {}
        # perfect nightly sweeps: the most cash that stays non-negative until 08:00 given known flows
        simcash = cash0 - u2 - u3 + (1 - h2) * s2 + (1 - h3) * s3
        repo = 0.0
        for t in range(start, end):
            if m.mature[t] and repo > 0:
                simcash += repo; repo = 0.0
            simcash += m.dep[t] - m.wd[t]
            if m.sweep[t] and simcash > 0:
                k = t + 1
                run, low = 0.0, 0.0
                while k < end and not m.mature[k]:
                    run += m.dep[k] - m.wd[k]; low = min(low, run); k += 1
                s = max(0.0, simcash + low - cfg["reserve"]["min_cash_usd"])
                sweeps[t] = s; repo += s; simcash -= s
        return {"bonds": b0 + u2 - s2, "equities": e0 + u3 - s3,
                "cash_delta": -u2 - u3 + (1 - h2) * s2 + (1 - h3) * s3,
                "haircut_cost": h2 * s2 + h3 * s3, "sweeps": sweeps,
                "trades": {"buy_bonds": u2, "sell_bonds": s2, "buy_equities": u3, "sell_equities": s3}}

    # ---------- attribution (FR-22) ----------
    def attribution(self, t1: int, t2: int, cfg1: dict, cfg2: dict | None = None) -> dict:
        cfg2 = cfg2 or cfg1
        r1, c1, i1 = self.reserve(t1, cfg1, parts=True)
        r2, c2, i2 = self.reserve(t2, cfg2, parts=True)
        steps = [{"component": k, "delta": c2[k] - c1[k]} for k in c1]
        return {"from": r1, "to": r2, "steps": steps, "parts_from": c1, "parts_to": c2,
                "info_from": i1, "info_to": i2, "check": float(r2 - r1 - sum(s["delta"] for s in steps))}
