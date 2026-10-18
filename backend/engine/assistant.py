"""Built-in treasury assistant: answers questions about the project without any external AI service.

It reads the question, picks an intent, pulls out entities (dates, amounts, durations, shock sizes) and
answers from the same engines the screens use. Questions about concepts are matched against a small
knowledge base written from the PRD. Every number comes from the simulation, with decision IDs where
a run was logged.
"""
from __future__ import annotations

import re
from datetime import datetime

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

KB = [
    ("What Reserve Juggler is",
     "Reserve Juggler AI is a treasury engine for hourly cash-flow data (the demo file has 8,760 rows for 2026; you can upload your own or "
     "stream live rows). Every hour it decides how much cash "
     "to keep liquid for payouts and where the rest can earn: cash, overnight repo, bonds and equities, with an overdraft line as backup. "
     "It forecasts flows with calibrated bands, sizes a reserve from outflow risk, picks the cheapest funding, and logs every decision. "
     "It recommends and records decisions; it never moves real money or connects to a bank by itself."),
    ("The five tiers",
     "Tier −1 is the overdraft (12% APR, immediate, up to a policy limit, by default 10% of starting wealth). Tier 0 is cash (earns nothing, "
     "immediate). Tier 1 is overnight repo: swept in at 17:00, back at 08:00, early redemption costs a 0.5% haircut. Tier 2 is bonds "
     "(duration 1.92, 2% haircut to sell). Tier 3 is equities (18% volatility, 15% haircut to sell). Rates and haircuts come from the dataset; "
     "starting balances are assumptions."),
    ("Night-lock and the 17:00 sweep",
     "Cash swept into repo at 17:00 is locked until 08:00, 15 hours later. The night-lock meter tests sweep sizes against simulated overnight "
     "outflow paths. It keeps an overnight reserve at the chosen confidence level plus a buffer, checks that the chance of breaching the "
     "overdraft limit stays under the policy cap (0.1%), and picks the sweep that maximises carry minus expected overdraft cost."),
    ("Borrow versus sell (break-even)",
     "Borrowing on the 12% overdraft costs about 0.00137% per hour. Selling costs a one-time haircut. They break even after "
     "h* = haircut ÷ (APR ÷ 8,760) hours: 365 hours for repo (0.5%), 1,460 hours for bonds (2%) and 10,950 hours for equities (15%). "
     "So for any gap shorter than about two weeks, borrowing beats breaking the repo early. The funding ladder ranks every path by total cost."),
    ("How the reserve is calculated",
     "The reserve covers the worst cumulative outflow inside the liquidity horizon (hours to the next 08:00 maturity plus 24), at the chosen "
     "confidence level (default 99%), from block-bootstrap scenario paths. It is split into expected peak outflow (median path), an "
     "uncertainty add-on (quantile minus median), momentum from the current flow surprise, committed payments, an operational buffer and "
     "minimum cash. The parts add up exactly, which is what the reserve waterfall shows."),
    ("Forecasting and conformal bands",
     "Forecasts cover deposits, withdrawals and net flow 1, 6 and 24 hours ahead, using gradient-boosted median models with hour, weekday, "
     "lag and rolling features. They are trained on the first two thirds of the data, calibrated on the next sixth and scored on the last "
     "sixth (January–August, September–October and November–December for the demo file). In live mode new hours extend the test window. "
     "Split conformal prediction turns errors into bands with tested coverage, and adaptive conformal inference adjusts them as errors drift."),
    ("Block bootstrap scenarios",
     "Scenario paths resample whole blocks of history (average 24 hours) starting at the same hour of the week, instead of independent hours. "
     "That keeps the clustering of outflows: lag-1 autocorrelation stays near the observed 0.695, while independent sampling would give about 0.41."),
    ("The circuit breaker",
     "The circuit breaker switches the optimizer to a conservative mode when inputs can't be trusted: stale data (over 2 hours), forecast band "
     "coverage drifting below 93% over 7 days or below 75% over 24 hours, monthly drift (PSI) above 0.25, or an infeasible optimizer. "
     "Conservative mode uses a 99.9% overnight reserve, a doubled buffer and no new bond or equity purchases."),
    ("The allocation optimizer",
     "A linear program (scipy HiGHS) re-solved daily chooses target balances for each tier. It maximises expected return over 30 days minus "
     "selling costs, subject to: cash plus undrawn overdraft covering the reserve, an operating cash floor, stressed 24-hour and 72-hour "
     "coverage, and caps of 30% bonds and 10% equities. Equity returns are risk-adjusted using the dataset's 18% volatility."),
    ("Reverse stress test",
     "The reverse stress test searches for the smallest shock that breaks the plan, meaning a fire sale or an unfunded payout, over 72 hours "
     "from the most vulnerable hour of the replay. It scans a grid on two axes (for example withdrawal multiplier and deposit delay), finds the "
     "boundary by bisection and re-runs boundary points in the Digital Twin to confirm them."),
    ("Digital Twin",
     "The Digital Twin injects shocks without touching the dataset: withdrawal spikes, deposit delays, equity drops, bond yield shocks, extra "
     "haircuts, a missed repo maturity or a stale data feed. It replays the optimized policy from the ledger at that hour and compares the "
     "result with the same window unshocked."),
    ("Wrong-way risk",
     "Wrong-way risk asks whether big outflows coincide with market drops, which would make selling assets worse exactly when cash is needed. "
     "The check compares equity returns during the top 5% outflow hours with all other hours using a block bootstrap. In this dataset there is "
     "no significant link, so scenarios sample flows and markets independently."),
    ("Policy backtest and perfect hindsight",
     "The backtest replays four policies on the test window (November–December for the demo) from the same starting ledger: conservative "
     "(all cash), fixed reserve (keep $200M, "
     "sell before borrowing), optimized, and perfect hindsight, which knows the future and is a ceiling. Capture is the optimized policy's gain "
     "over all-cash divided by perfect hindsight's gain. All policies hold the same falling equities, so compare the gaps, not the totals."),
    ("Price of safety",
     "The price of safety replays the optimized policy at reserve confidence levels from 90% to 99.9% and shows what each extra level costs in "
     "return. In this dataset overnight risk is small next to the overdraft line, so extra safety is cheap."),
    ("Dataset and honesty report",
     "Every dataset passes six checks before it is used (columns, missing values, unique and continuous hours, net = deposits − withdrawals, "
     "no negative flows). The demo file has 8,760 hourly rows for 2026 and its honesty report flags signs it is generated: constant columns, "
     "perfectly regular repo flags, an invariant that holds to the cent and repeated daily shapes. Ask \"tell me about the dataset\" for the "
     "active one."),
    ("Bring your own data",
     "An admin can upload any CSV of hourly (or finer) cash flows on the Data screen. The app guesses which column is the date, deposits, "
     "withdrawals or net flow, lets you correct the mapping, adds up transactions into hours, fills missing hours with zero flows, and uses "
     "standard values for market columns you don't have (marked as assumed). It needs at least 60 days. Activating a dataset retrains the "
     "models; starting balances and policy amounts can be scaled to the size of your flows."),
    ("Live mode",
     "In live mode new hourly rows arrive through the API (POST /api/live/rows with an API token, or the live_feed tool). Each batch is "
     "appended to the active dataset, the engine re-scores on its current models within seconds, and it fully retrains after a set number of "
     "new hours (24 by default). The dashboard follows the newest hour. If the feed goes quiet for longer than the policy's stale limit, "
     "the circuit breaker switches to conservative mode."),
    ("Accounts and roles",
     "Everyone signs in. Roles: admin (users, policy, balances, datasets), operator (runs the desk and requests approvals), approver (approves "
     "other people's requests), analyst (stress tests), viewer (read-only) and ingest (machines pushing live rows). Passwords are stored as "
     "salted PBKDF2 hashes, sessions expire, repeated wrong passwords lock the account for five minutes, and every sign-in is in the audit log."),
    ("Policy as code and maker-checker",
     "Treasury rules live in a versioned YAML policy (reserve confidence, buffers, caps, overdraft limit, night-lock cap, breaker thresholds). "
     "Every save creates a new hashed version and re-runs the replays. Transfers above the approval threshold ($25M by default) need a second, "
     "different user to approve; nobody can approve their own request."),
    ("Audit log",
     "Every decision is written to a hash-chained audit log: each record stores SHA-256 of the previous hash plus its own content, so editing any "
     "record breaks verification from that point. Decisions store the dataset hash, policy version, model version and seed, so they can be "
     "reproduced exactly. The tamper test on the Trust screen demonstrates this."),
    ("Replay",
     "Replay plays the whole year hour by hour at up to 500 hours per second, comparing the optimized and fixed-reserve policies, with circuit "
     "breaker hours shaded and every alert clickable to jump to that hour."),
    ("Live Command Center",
     "The Command Center's sliders (withdrawals, deposits, repo rate, reserve confidence) recalculate the next 24 hours in the browser from real "
     "inputs: the dataset's average flow for each hour of the week, the engine's reserve at each confidence level and the simulated ledger. "
     "Go live plays those hours forward; Request approval files a real maker-checker request."),
]

# extra words people use for each topic, to help retrieval match everyday phrasing
ALIASES = {
    "What Reserve Juggler is": "project about overview purpose app product what does it do idea goal summary",
    "The five tiers": "tiers tier levels buckets accounts where money kept overdraft cash repo bonds equities stocks assets",
    "Night-lock and the 17:00 sweep": "night lock overnight lock sweep locked 5pm evening repo window",
    "Borrow versus sell (break-even)": "break even breakeven borrow sell overdraft haircut cheaper funding ladder cost",
    "How the reserve is calculated": "reserve calculation computed formula buffer floor required cash quantile confidence",
    "Forecasting and conformal bands": "forecast model prediction predict machine learning ml gradient boosting conformal interval band uncertainty accuracy",
    "Block bootstrap scenarios": "bootstrap scenario paths simulation monte carlo resampling autocorrelation clustering",
    "The circuit breaker": "breaker safety switch fallback conservative mode alarm stale drift trip tripped",
    "The allocation optimizer": "optimizer optimiser linear program lp allocation rebalance target weights caps highs",
    "Reverse stress test": "reverse stress breaking point break limit smallest shock boundary fail",
    "Digital Twin": "digital twin what if scenario shock simulate stress test injection",
    "Wrong-way risk": "wrong way correlation markets crash outflows together dependence",
    "Policy backtest and perfect hindsight": "backtest policies compare oracle hindsight benchmark capture fixed conservative optimized",
    "Price of safety": "price safety cost confidence 99 percent quantile tradeoff",
    "Dataset and honesty report": "dataset data csv rows columns synthetic generated fake real honesty quality checks",
    "Policy as code and maker-checker": "approve approves approval approver sign off second person maker checker transfer transfers threshold policy yaml rules governance",
    "Audit log": "audit log hash chain sha tamper tampering verify reproduce reproducible history record",
    "Replay": "replay time travel year playback animation scrubber",
    "Live Command Center": "live sliders command center dashboard real time adjust controls",
    "Bring your own data": "upload import my own data csv file excel bank export mapping columns custom dataset bring",
    "Live mode": "live feed streaming real time ingest api webhook new rows retrain retraining stale",
    "Accounts and roles": "login log in sign in password account user users roles permissions security admin access",
}

HELP = ("I can answer questions about this project using its own simulation. Try:\n"
        "- **Status**: \"How are we doing on 20 Nov at 10:00?\"\n"
        "- **Funding**: \"Cheapest way to cover $50M for 12 hours?\"\n"
        "- **Sweep**: \"How much should we sweep into repo on 5 Dec?\"\n"
        "- **What-if**: \"Can we survive 3x withdrawals for 48 hours?\"\n"
        "- **Breaking point**: \"What's the smallest shock that breaks us?\"\n"
        "- **Why**: \"Why did the reserve change?\"\n"
        "- **Results**: \"How did the optimized policy do?\", \"How accurate is the forecast?\"\n"
        "- **Concepts**: \"What is night-lock?\", \"How is the reserve calculated?\"")

INTENTS = {
    "greet": (["hi", "hello", "hey", "thanks", "thank you", "good morning"], []),
    "help": (["help", "what can you", "how do i use", "commands", "examples"], []),
    "scenario": (["survive", "what if", "what happens", "shock", "spike", "surge", "stress", "crash", "run on", "withdrawals for"], ["x", "×", "times"]),
    "reverse": (["breaking point", "break us", "breaks us", "smallest shock", "reverse stress", "how much can we take", "fail at", "margin of safety", "safety margin",
                 "where does it break", "when does it break", "does it break", "what breaks", "break point"], []),
    "ladder": (["cheapest", "cover", "gap", "fund", "borrow", "overdraft", "shortfall", "raise cash", "liquidate", "sell bonds", "sell equities"], []),
    "sweep": (["sweep", "repo tonight", "night-lock", "night lock", "overnight", "17:00", "carry"], []),
    "allocation": (["why", "allocation", "optimizer", "optimiser", "binding", "target", "reserve change", "reserve move", "attribution", "rebalance"], []),
    "backtest": (["backtest", "policy", "policies", "perform", "performance", "fixed reserve", "compare", "hindsight", "oracle", "capture", "return", "price of safety",
                  "how did", "vs fixed", "versus fixed", "against fixed", "beat", "beats", "results", "better than"], []),
    "forecast": (["forecast", "accuracy", "accurate", "wape", "error", "coverage", "band", "predict", "drift test", "conformal"], []),
    "data": (["dataset", "data", "csv", "rows", "synthetic", "honest", "honesty", "checks", "quality", "psi"], []),
    "status": (["status", "how are we", "how is", "cash", "reserve", "runway", "surplus", "now", "today", "doing", "balance", "breaker", "position"], []),
}
CONCEPT_CUES = ["what is", "what's", "what are", "explain", "how does", "how is", "how do", "define", "meaning", "tell me about", "why does", "difference"]


def _m(x: float) -> str:
    a, s = abs(x), "−" if x < 0 else ""
    if a >= 1e9:
        return f"{s}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a/1e6:,.1f}M"
    if a >= 1e3:
        return f"{s}${a/1e3:,.1f}K"
    return f"{s}${a:,.0f}"


class Assistant:
    def __init__(self, app):
        self.app = app
        self.sessions: dict[str, dict] = {}
        docs = [f"{t}. {t}. {x} {ALIASES.get(t, '')}" for t, x in KB]
        self.vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
        self.kb_matrix = self.vec.fit_transform(docs)

    # ---------- entity extraction ----------
    def _time(self, q: str, default_t: int) -> tuple[int, bool]:
        ts = self.app.ts_str
        hour = None
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", q)
        if m:
            hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        else:
            m = re.search(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", q)
            if m:
                hour = int(m.group(1))
        date, year = None, int(ts[default_t][:4])
        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
        if m:
            year, date = int(m.group(1)), (int(m.group(2)), int(m.group(3)))
        else:
            m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", q)
            if m:
                date = (MONTHS[m.group(2)], int(m.group(1)))
            else:
                m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", q)
                if m:
                    date = (MONTHS[m.group(1)], int(m.group(2)))
        if date is None and hour is None:
            return default_t, False
        base = ts[default_t]
        try:
            d = datetime(year, date[0], date[1]) if date else datetime.strptime(base[:10], "%Y-%m-%d")
        except ValueError:
            return default_t, False
        h = hour if hour is not None else (9 if date else int(base[11:13]))
        key = f"{d:%Y-%m-%d} {h:02d}:00"
        return (ts.index(key), True) if key in ts else (default_t, False)

    @staticmethod
    def _entities(q: str) -> dict:
        e = {}
        m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*(b|bn|billion|m|mn|mm|million|k|thousand)\b", q)
        if m:
            mult = {"b": 1e9, "bn": 1e9, "billion": 1e9, "m": 1e6, "mn": 1e6, "mm": 1e6, "million": 1e6, "k": 1e3, "thousand": 1e3}[m.group(2)]
            e["amount"] = float(m.group(1)) * mult
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:x|×|times)\b", q)
        if m:
            e["mult"] = float(m.group(1))
        else:
            m = re.search(r"\b(double|doubles|doubled|twice|triple|triples|tripled|quadruple|quadruples|quadrupled)\b", q)
            if m:
                e["mult"] = {"dou": 2.0, "twi": 2.0, "tri": 3.0, "qua": 4.0}[m.group(1)[:3]]
        m = re.search(r"(\d+)\s*(?:h|hr|hrs|hour|hours)\b", q)
        if m:
            e["hours"] = int(m.group(1))
        m = re.search(r"(\d+)\s*(?:day|days)\b", q)
        if m:
            e["hours"] = int(m.group(1)) * 24
        m = re.search(r"(?:equit\w*|stocks?|market)[^\d]{0,20}(\d+(?:\.\d+)?)\s*%", q) or re.search(r"(\d+(?:\.\d+)?)\s*%[^\d]{0,20}(?:equit\w*|stocks?|crash|drop)", q)
        if m:
            e["equity_drop"] = float(m.group(1)) / 100
        m = re.search(r"(\d+)\s*(?:bp|bps|basis points)", q)
        if m:
            e["yield_bp"] = float(m.group(1))
        m = re.search(r"deposits?[^\d]{0,25}(\d+)\s*(?:h|hr|hrs|hours)", q) or re.search(r"delay[^\d]{0,15}(\d+)", q)
        if m:
            e["delay"] = int(m.group(1))
        if "missed" in q and "maturity" in q:
            e["missed"] = True
        return e

    def _intent(self, q: str, ents: dict) -> str | None:
        scores = {}
        for name, (words, extra) in INTENTS.items():
            s = sum(2 if " " in w else 1 for w in words if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", q))
            if name == "scenario" and "mult" in ents:
                s += 2
            if name == "ladder" and "amount" in ents and "hours" in ents:
                s += 2
            if s:
                scores[name] = s
        if not scores:
            return None
        return max(scores, key=lambda k: (scores[k], -list(INTENTS).index(k)))

    def _concept(self, q: str) -> tuple[float, int]:
        sims = cosine_similarity(self.vec.transform([q]), self.kb_matrix)[0]
        i = int(np.argmax(sims))
        return float(sims[i]), i

    # ---------- main entry ----------
    def ask(self, question: str, t: int, session: str = "default") -> dict:
        q = " ".join(question.lower().strip().split())
        ctx = self.sessions.setdefault(session, {})
        t_q, has_time = self._time(q, t)
        if not has_time and ctx.get("intent") and is_follow_up(q):
            t_q = ctx.get("t", t)          # a short follow-up keeps the hour being discussed
        ents = self._entities(q)
        intent = self._intent(q, ents)
        concept_q = any(q.startswith(c) or f" {c} " in f" {q} " for c in CONCEPT_CUES)
        sim, kb_i = self._concept(q)

        # "What is night-lock?" is a concept question even though it mentions a feature by name
        soft = (None, "status", "sweep", "backtest", "forecast", "data", "allocation")
        if concept_q and sim > 0.12 and intent in soft and not has_time and not ents:
            intent = "concept"
        elif intent is None:
            if ctx.get("intent") and (ents or has_time) and is_follow_up(q):
                intent = ctx["intent"]
                ents = {**ctx.get("ents", {}), **ents}
            elif sim > 0.07:
                intent = "concept"
        # a short follow-up that names the previous topic stays on it ("what if the gap lasts 30 days")
        prev = ctx.get("intent")
        if prev in INTENTS and is_follow_up(q) and intent != prev:
            words = INTENTS[prev][0]
            if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", q) for w in words):
                intent = prev
        if intent in ("scenario", "ladder") and prev == intent and is_follow_up(q):
            ents = {**ctx.get("ents", {}), **ents}

        handler = {"greet": self.greet, "help": lambda *_: (HELP, []), "status": self.status, "ladder": self.ladder,
                   "sweep": self.sweep, "scenario": self.scenario, "reverse": self.reverse, "allocation": self.allocation,
                   "backtest": self.backtest, "forecast": self.forecast, "data": self.data,
                   "concept": lambda *_: self.concept(kb_i)}.get(intent)
        if handler is None:
            return {"answer": "That's outside what I know. I only answer questions about Reserve Juggler, its data and its simulations.\n" + HELP,
                    "intent": None, "runs": [], "suggestions": self._suggest(None)}
        answer, runs = handler(t_q, ents)
        if intent not in ("greet", "help", "concept"):
            ctx.update({"t": t_q, "intent": intent, "ents": ents})
        sugg = self._suggest(intent)
        if intent == "scenario":
            sugg = [f"And at {ents.get('mult', 2.0) + 1:g}x?"] + sugg[1:]
        return {"answer": answer, "intent": intent, "runs": runs, "as_of": self.app.ts_str[t_q], "suggestions": sugg}

    @staticmethod
    def _suggest(intent):
        s = {"status": ["Why did the reserve change?", "How much should we sweep tonight?", "Can we survive 2x withdrawals?"],
             "ladder": ["What is the break-even rule?", "What if the gap lasts 30 days?", "How much should we sweep tonight?"],
             "sweep": ["What is night-lock?", "Can we survive 3x withdrawals for 24 hours?", "How are we doing now?"],
             "scenario": ["And at 4x?", "What's the smallest shock that breaks us?", "Cheapest way to cover $100M for 12 hours?"],
             "reverse": ["Can we survive 2x withdrawals for 48 hours?", "What is the Digital Twin?", "How did the optimized policy do?"],
             "allocation": ["How is the reserve calculated?", "What are the five tiers?", "How are we doing now?"],
             "backtest": ["What is the price of safety?", "How accurate is the forecast?", "Is the data synthetic?"],
             "forecast": ["What are conformal bands?", "Run the drift test", "How did the optimized policy do?"],
             "data": ["How accurate is the forecast?", "What is wrong-way risk?", "How did the optimized policy do?"]}
        return s.get(intent, ["How are we doing now?", "Cheapest way to cover $50M for 12 hours?", "What is night-lock?"])

    # ---------- handlers ----------
    def greet(self, t, e):
        return ("Hi! I'm the Reserve Juggler assistant. I answer from this project's own simulation, so every number is real "
                "for the dataset. " + HELP), []

    def concept(self, i):
        title, text = KB[i]
        return f"**{title}**\n{text}", []

    def status(self, t, e):
        o = self.app.overview(t)
        s, run = o["state"], o["runway"]["hours"]
        rw = lambda h: "more than 72 h" if h is None else f"{h} h"
        lines = [f"**As of {o['ts']}** the position is **{o['status']}**.",
                 f"- Cash {_m(s['cash'])} against a required reserve of {_m(o['reserve'])} ({o['reserve_info']['quantile']:.0%} confidence), "
                 f"so {'a surplus' if o['surplus'] >= 0 else 'a shortfall'} of {_m(abs(o['surplus']))}.",
                 f"- Repo {_m(s['repo'])}, bonds {_m(s['bonds'])}, equities {_m(s['equities'])}, overdraft drawn {_m(s['od'])} of {_m(s['od_limit'])}.",
                 f"- Runway on the worst 1% of paths: {rw(run['p99'])}.",
                 f"- Circuit breaker: {'ON (' + o['breaker']['reason'] + ')' if o['breaker']['on'] else 'off'}.",
                 f"- Next action: {o['next_action']['title']}. {o['next_action']['detail']}."]
        return "\n".join(lines), []

    def ladder(self, t, e):
        gap, dur = e.get("amount", 50e6), e.get("hours", 12)
        r = self.app.ladder(t, gap, dur, "assistant")
        feas = [p for p in r["paths"] if p["feasible"]]
        best = r["recommended"]
        lines = [f"**Cover {_m(gap)} for {dur} h** (as of {self.app.ts_str[t]}): **{best['path']}**, costing {_m(best['cost'])}."]
        for p in feas[1:3]:
            lines.append(f"- Next: {p['path']}: {_m(p['cost'])} ({p['note']})")
        lines.append(f"- Overdraft headroom {_m(r['headroom'])}; repo matures in {r['hours_to_maturity']} h.")
        be = r["breakeven_hours"]
        lines.append(f"Borrowing beats selling until {be['repo']:,.0f} h for repo, {be['bonds']:,.0f} h for bonds and {be['equities']:,.0f} h for equities. (decision #{r['decision']['id']})")
        return "\n".join(lines), [r["decision"]["id"]]

    def sweep(self, t, e):
        r = self.app.nightlock(t, "assistant")
        h = r["history"]
        if r["sweep"] < 1e5:
            why = ("cash before the sweep is negative, so the overdraft is already covering payouts"
                   if r["cash"] < 0 else "cash before the sweep doesn't exceed the overnight reserve")
            return (f"**No sweep at {r['ts']}**: {why}.\n- Cash before the sweep {_m(r['cash'])}; overnight reserve {_m(r['night_reserve'])}.\n"
                    f"- Everything stays in cash tonight; carry $0. (decision #{r['decision']['id']})"), [r["decision"]["id"]]
        lines = [f"**Sweep {_m(r['sweep'])} into repo at {r['ts']}.**",
                 f"- Cash before the sweep {_m(r['cash'])}; keeps {_m(r['night_reserve'])} overnight.",
                 f"- Carry {_m(r['carry'])} at {r['rate']:.2%}; overdraft chance {r['od_prob']:.2%}; beyond-limit chance {r['breach_prob']:.3%} (cap {r['cap']:.1%}).",
                 f"- Over the year: beyond-limit nights predicted {h['expected_breach_nights']:.1f}, observed {h['observed_breach_nights']}. (decision #{r['decision']['id']})"]
        return "\n".join(lines), [r["decision"]["id"]]

    def scenario(self, t, e):
        hours = min(max(e.get("hours", 48), 6), 168)
        shocks = {"start": t, "hours": hours, "withdrawal_mult": e.get("mult", 2.0), "deposit_delay_h": e.get("delay", 0),
                  "equity_drop": e.get("equity_drop", 0.0), "yield_shock_bp": e.get("yield_bp", 0.0), "missed_maturity": e.get("missed", False)}
        r = self.app.twin(shocks, "assistant")
        s, b = r["metrics"]["shocked"], r["metrics"]["baseline"]
        desc = [f"{shocks['withdrawal_mult']:g}× withdrawals"]
        if shocks["deposit_delay_h"]:
            desc.append(f"deposits delayed {shocks['deposit_delay_h']} h")
        if shocks["equity_drop"]:
            desc.append(f"equities −{shocks['equity_drop']:.0%}")
        if shocks["yield_shock_bp"]:
            desc.append(f"yields +{shocks['yield_shock_bp']:.0f} bp")
        if shocks["missed_maturity"]:
            desc.append("a missed repo maturity")
        if s["shortfall_hours"] > 0:
            verdict = f"**No: payouts go unfunded for {s['shortfall_hours']} h** (largest gap {_m(s['max_shortfall'])})."
        elif s["forced_sales"] > 1:
            verdict = f"**Yes, but only by selling {_m(s['forced_sales'])} of bonds or equities** (haircuts {_m(s['haircut_cost'])})."
        elif s["peak_od"] > 0:
            verdict = f"**Yes, on the overdraft**: peak {_m(s['peak_od'])}, interest {_m(s['overdraft_cost'])}."
        else:
            verdict = "**Yes, comfortably**: cash and repo cover it."
        lines = [f"Scenario from {self.app.ts_str[t]} for {hours} h with {', '.join(desc)}:", verdict,
                 f"- Lowest cash + repo {_m(s['min_liquid'])} (baseline {_m(b['min_liquid'])}).",
                 f"- Peak overdraft {_m(s['peak_od'])} (baseline {_m(b['peak_od'])}); circuit breaker on for {r['breaker_hours']} h. (decision #{r['decision']['id']})"]
        return "\n".join(lines), [r["decision"]["id"]]

    def reverse(self, t, e):
        r = self.app.reverse("withdrawal_mult", "deposit_delay_h", None, "assistant")
        lines = [f"**Starting from the most vulnerable hour ({r['start_ts']}), the plan breaks at {r['margin']}× withdrawals** over {r['window']} h with no deposit delay.",
                 f"- With deposits also delayed {r['ys'][-1]:.0f} h it breaks at {r['boundary'][-1]}×." if r["boundary"][-1] else "- Even with long deposit delays it holds in the tested range.",
                 f"- Starting cash + repo {_m(r['start_state']['cash'] + r['start_state']['repo'])}. Breaking means a fire sale or an unfunded payout.",
                 f"- {sum(c['fails'] for c in r['confirmed'])} of {len(r['confirmed'])} boundary points confirmed in the Digital Twin."]
        return "\n".join(lines), []

    def allocation(self, t, e):
        a = self.app.allocation(t, "assistant")
        if not a["feasible"]:
            return f"At {self.app.ts_str[t]} the optimizer has no feasible allocation, so the circuit breaker falls back to the conservative policy.", []
        att = a["attribution"]
        steps = sorted(att["steps"], key=lambda s: -abs(s["delta"]))[:3]
        moves = [(k, a[k]) for k in ("buy_bonds", "sell_bonds", "buy_equities", "sell_equities") if a[k] > 1e5]
        lines = [f"**Optimizer at {self.app.ts_str[t]}:** " + (", ".join(f"{k.replace('_', ' ')} {_m(v)}" for k, v in moves) if moves else "hold the current allocation") + ".",
                 f"- Binding limits: {', '.join(a['binding']) or 'none'}.",
                 f"- **Reserve moved {_m(att['from'])} → {_m(att['to'])}** since {a['attribution_from']}, mainly:"]
        lines += [f"  - {s['component'].replace('_', ' ')}: {'+' if s['delta'] >= 0 else ''}{_m(s['delta'])}" for s in steps]
        lines.append(f"(decision #{a['decision']['id']})")
        return "\n".join(lines), [a["decision"]["id"]]

    def backtest(self, t, e):
        b = self.app.backtest_view()
        ms = {m["policy"]: m for m in b["metrics"]}
        opt, fix, cons = ms["optimized"], ms["fixed"], ms["conservative"]
        diff = opt["net_return"] - fix["net_return"]
        sign = lambda x: ("+" if x >= 0 else "") + _m(x)
        lines = [f"**Optimized: {sign(diff)} vs the fixed reserve, {opt['shortfall_hours']} gap hours** "
                 f"({b['window'][0][:10]} → {b['window'][1][:10]}, same starting ledger for every policy).",
                 f"- {sign(opt['net_return'] - cons['net_return'])} vs keeping everything in cash.",
                 f"- Idle cash {_m(opt['idle_cash'])} vs {_m(fix['idle_cash'])} for the fixed reserve.",
                 f"- Captures {opt['capture']:.0%} of the gain perfect hindsight finds (fixed: {fix['capture']:.0%})." if opt["capture"] is not None else "",
                 f"- Gap hours: optimized {opt['shortfall_hours']}, fixed {fix['shortfall_hours']}, all cash {cons['shortfall_hours']}.",
                 "Gains compare liquidity decisions on identical flows; totals including market moves are negative for every policy because they all hold the same falling equities."]
        lines = [x for x in lines if x]
        pos = b["price_of_safety"]
        lines.append(f"Price of safety: going from {pos[0]['q']:.0%} to {pos[-1]['q']:.1%} confidence costs {_m(pos[0]['net_return'] - pos[-1]['net_return'])}.")
        return "\n".join(lines), []

    def forecast(self, t, e):
        fc = self.app.fc
        lines = [f"**Forecast accuracy on the test window (from {fc.bounds['test'][:10]}), WAPE, lower is better:**"]
        for m in fc.metrics:
            if m["h"] in (1, 24):
                lines.append(f"- {m['target']} {m['h']} h ahead: model {m['model_wape']:.3f} vs same-hour-yesterday {m['naive_wape']:.3f} ({1 - m['model_wape']/m['naive_wape']:.0%} less error)")
        c = fc.coverage[1]
        dt = self.app.drift_test(2.0)
        lines.append(f"Calibration (1 h net flow): 95% band covered {c['static_95']:.1%}, 99% band {c['static_99']:.1%}, adaptive band {c['aci_95']:.1%}.")
        lines.append(f"Drift test: doubling the error variance trips the coverage alarm after {dt['hours_to_alarm']} h (target within 48 h).")
        return "\n".join(lines), []

    def data(self, t, e):
        meta, h = self.app.meta, self.app.honesty
        flagged = [f for f in h["findings"] if f["flag"]]
        w = self.app.wrongway
        lines = [f"**Dataset:** {meta['rows']:,} hourly rows, {meta['start'][:10]} to {meta['end'][:10]}, {sum(c['ok'] for c in meta['checks'])} of {len(meta['checks'])} data checks pass (sha256 {meta['sha256'][:12]}).",
                 f"- The honesty report flags {len(flagged)} signs of generated data, e.g. {flagged[0]['check'].lower()}: {flagged[0]['result']}." if flagged else "- The honesty report found no signs of generated data.",
                 f"- Annual net flow {_m(self.app.summary['cum_net'])}; {self.app.summary['neg_share']:.1%} of hours are net outflows.",
                 f"- Worst monthly drift (PSI) {max(self.app.psi.values()):.3f}, well under the 0.25 alarm.",
                 f"- Wrong-way risk: {'a significant link' if w['significant'] else 'no significant link'} between big outflows and equity drops.",
                 "Safe to claim: comparisons on this exact year. Not safe: real-world performance or rare annual events."]
        return "\n".join(lines), []


def is_follow_up(q: str) -> bool:
    return len(q.split()) <= 7 or q.startswith(("and ", "what about", "how about", "now ", "same ", "then ", "also "))
