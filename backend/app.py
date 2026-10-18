"""Reserve Juggler AI — FastAPI service. Run: python backend/app.py  (then open http://localhost:8000)

The server starts at once and builds the engine in the background; API calls answer 503 until it is ready.
Everyone signs in (cookie session in the browser, bearer API token for scripts); each route checks a role.
"""
from __future__ import annotations

import json
import logging
import math
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import uvicorn
import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))

import settings as settings_mod  # noqa: E402

settings_mod.load_env()

from engine import datasets as ds  # noqa: E402
from engine.audit import Store  # noqa: E402
from engine.auth import ROLE_HELP, Auth, AuthError, can  # noqa: E402
from engine.manager import Engine  # noqa: E402
from engine.treasury import DEFAULT_POLICY, POLICY_LABELS  # noqa: E402

S = settings_mod.load()
logging.basicConfig(level=S.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rj.api")
S.data_dir.mkdir(parents=True, exist_ok=True)

store = Store(S.db_path)
auth = Auth(store, S.session_hours)
registry = ds.Registry(S.datasets_dir, S.demo_csv)
engine = Engine(store, registry, S.cache_dir, S.retrain_every_hours, S.live_seconds_per_hour)
active = store.get_kv("active_dataset") or "demo"
engine.request(active if registry.get(active) else "demo", reason="startup")

api = FastAPI(title="Reserve Juggler AI", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
COOKIE = "rj_session"


# ---------- JSON helpers ----------
def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def J(o, status: int = 200) -> Response:
    return Response(json.dumps(clean(o), separators=(",", ":")), status_code=status, media_type="application/json")


def C():
    """The current engine, or 503 while it is still being built."""
    core = engine.core
    if core is None:
        raise HTTPException(503, engine.message if engine.state != "error" else engine.message)
    return core


def T(t: int | None, core=None) -> int:
    core = core or C()
    n = len(core.df)
    if t is None:
        return core.default_t
    if not 0 <= t < n:
        raise HTTPException(400, f"t must be in 0..{n - 1}")
    return t


# ---------- rate limits ----------
class RateLimit:
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str, limit: int, per: float = 60.0):
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > per:
                q.popleft()
            if len(q) >= limit:
                raise HTTPException(429, f"Too many requests; try again in {int(per - (now - q[0])) + 1} s")
            q.append(now)


limits = RateLimit()


def client_ip(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd and ip in S.trusted_proxies:
        ip = fwd.split(",")[0].strip()
    return ip


# ---------- authentication ----------
def current_user(request: Request) -> dict | None:
    token = None
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
    else:
        token = request.cookies.get(COOKIE)
        # cookie sessions: state-changing calls must carry the custom header, which other sites cannot send
        if token and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "rj":
            raise HTTPException(403, "Missing X-Requested-With header")
    user = auth.resolve(token)
    request.state.user = user["username"] if user else None
    return user


def need(*roles: str):
    """Dependency: signed-in user holding one of `roles` (admins always pass; no roles = any signed-in user)."""
    def dep(request: Request) -> dict:
        user = current_user(request)
        if not user:
            raise HTTPException(401, "Sign in to continue")
        if not can(user, *roles):
            raise HTTPException(403, f"This needs the {' or '.join(roles)} role")
        return user
    return dep


ANY = need()


def set_session(resp: Response, token: str):
    resp.set_cookie(COOKIE, token, max_age=S.session_hours * 3600, httponly=True, samesite="strict",
                    secure=S.secure_cookies, path="/")


# ---------- middleware: request log, security headers, error shield ----------
CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@api.middleware("http")
async def shell(request: Request, call_next):
    rid = secrets.token_hex(4)
    t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("unhandled error rid=%s %s %s", rid, request.method, request.url.path)
        resp = JSONResponse({"detail": f"Internal error (reference {rid}). It has been logged."}, status_code=500)
    ms = (time.time() - t0) * 1000
    if request.url.path.startswith("/api/"):
        log.info("%s %s %s %.0fms user=%s ip=%s rid=%s", request.method, request.url.path, resp.status_code, ms,
                 getattr(request.state, "user", None), client_ip(request), rid)
    resp.headers["X-Request-ID"] = rid
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers.setdefault("Content-Security-Policy", CSP)
    if S.secure_cookies:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ---------- health ----------
@api.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@api.get("/readyz", include_in_schema=False)
def readyz():
    st = engine.status()
    return J({"ready": engine.ready, **st}, 200 if engine.ready else 503)


# ---------- sign-in ----------
@api.get("/api/auth/status")
def auth_status(request: Request):
    try:
        user = current_user(request)
    except HTTPException:
        user = None
    return J({"setup_needed": auth.needs_setup(), "user": user, "public_demo": S.public_demo,
              "roles": ROLE_HELP, "engine": {"state": engine.state, "message": engine.message}})


@api.post("/api/auth/setup")
def auth_setup(request: Request, body: dict = Body(...)):
    limits.check(f"login:{client_ip(request)}", 10)
    try:
        u = auth.setup_first_admin(body.get("username", ""), body.get("password", ""))
        token, u = auth.login(u["username"], body.get("password", ""), client_ip(request))
    except AuthError as e:
        raise HTTPException(400, str(e))
    resp = J({"user": u})
    set_session(resp, token)
    return resp


@api.post("/api/auth/login")
def auth_login(request: Request, body: dict = Body(...)):
    limits.check(f"login:{client_ip(request)}", 20)
    try:
        token, u = auth.login(str(body.get("username", ""))[:60], str(body.get("password", ""))[:200], client_ip(request))
    except AuthError as e:
        raise HTTPException(429 if "Too many" in str(e) else 401, str(e))
    resp = J({"user": u})
    set_session(resp, token)
    return resp


@api.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        auth.logout(token)
    resp = J({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@api.get("/api/auth/me")
def auth_me(user: dict = Depends(ANY)):
    return J(user)


@api.post("/api/auth/password")
def change_password(body: dict = Body(...), user: dict = Depends(ANY)):
    try:
        auth.login(user["username"], body.get("current", ""), "password-change")   # re-check the current password
        auth.update_user(user["username"], user["username"], password=body.get("new", ""))
    except AuthError as e:
        raise HTTPException(400, str(e))
    return J({"ok": True, "note": "Password changed; other sessions were signed out. Sign in again."})


# ---------- users and API tokens (admin) ----------
@api.get("/api/users")
def list_users(user: dict = Depends(need("admin"))):
    return J({"users": auth.users(), "tokens": auth.tokens(), "roles": ROLE_HELP})


@api.post("/api/users")
def create_user(body: dict = Body(...), user: dict = Depends(need("admin"))):
    try:
        return J(auth.create_user(body.get("username", ""), body.get("password", ""), body.get("roles", []), user["username"]))
    except AuthError as e:
        raise HTTPException(400, str(e))


@api.patch("/api/users/{username}")
def update_user(username: str, body: dict = Body(...), user: dict = Depends(need("admin"))):
    try:
        return J(auth.update_user(username, user["username"], roles=body.get("roles"), active=body.get("active"),
                                  password=body.get("password")))
    except AuthError as e:
        raise HTTPException(400, str(e))


@api.post("/api/tokens")
def create_token(body: dict = Body(...), user: dict = Depends(need("admin"))):
    try:
        return J(auth.create_token(body.get("username") or user["username"], body.get("label", "token"), user["username"]))
    except AuthError as e:
        raise HTTPException(400, str(e))


@api.delete("/api/tokens/{token_id}")
def revoke_token(token_id: str, user: dict = Depends(need("admin"))):
    try:
        auth.revoke_token(token_id, user["username"])
    except AuthError as e:
        raise HTTPException(404, str(e))
    return J({"ok": True})


# ---------- engine views ----------
@api.get("/api/meta")
def meta(user: dict = Depends(ANY)):
    core = C()
    return J({"dataset": core.meta, "ts": core.ts_str, "default_t": core.default_t, "test_start": core.test_start,
              "policies": POLICY_LABELS, "boot_seconds": core.boot_seconds, "copilot": engine.copilot.enabled,
              "policy_version": core.policy_rec["version"], "balances": core.balances, "source": core.dataset,
              "live": core.live, "version": engine.version, "user": user, "demo_tools": S.demo_tools,
              "splits": core.fc.bounds})


@api.get("/api/overview")
def overview(t: int | None = None, user: dict = Depends(ANY)):
    core = C()
    with core.lock:
        return J(core.overview(T(t, core)))


@api.get("/api/data")
def data_view(user: dict = Depends(ANY)):
    core = C()
    return J({"summary": core.summary, "checks": core.meta["checks"], "honesty": core.honesty, "psi": core.psi,
              "wrongway": core.wrongway})


@api.get("/api/forecast")
def forecast(h: int = 1, start: int | None = None, user: dict = Depends(ANY)):
    if h not in (1, 6, 24):
        raise HTTPException(400, "h must be 1, 6 or 24")
    return J(C().forecast_view(h, start))


@api.post("/api/forecast/drift-test")
def drift(factor: float = Body(2.0, embed=True), user: dict = Depends(ANY)):
    if not 1.0 <= factor <= 10.0:
        raise HTTPException(400, "factor must be between 1 and 10")
    return J(C().drift_test(factor))


@api.get("/api/ladder")
def ladder(t: int | None = None, gap: float = 10e6, duration: float = 10, user: dict = Depends(ANY)):
    if not (0 < gap < 1e13 and 0 < duration <= 24 * 366):
        raise HTTPException(400, "gap must be positive and duration between 0 and 8,784 hours")
    core = C()
    with core.lock:
        return J(core.ladder(T(t, core), gap, duration, user["username"]))


@api.get("/api/nightlock")
def nightlock(t: int | None = None, user: dict = Depends(ANY)):
    core = C()
    with core.lock:
        return J(core.nightlock(T(t, core), user["username"]))


@api.get("/api/allocation")
def allocation(t: int | None = None, user: dict = Depends(ANY)):
    core = C()
    with core.lock:
        return J(core.allocation(T(t, core), user["username"]))


@api.post("/api/scenario")
def scenario(shocks: dict = Body(...), user: dict = Depends(need("analyst", "operator"))):
    core = C()
    shocks["start"] = T(int(shocks.get("start", core.default_t)), core)
    for k, lo, hi in (("withdrawal_mult", 0, 20), ("deposit_delay_h", 0, 168), ("equity_drop", 0, 1),
                      ("yield_shock_bp", -500, 1000), ("haircut_add_pct", 0, 50), ("hours", 1, 168)):
        if k in shocks and not lo <= float(shocks[k]) <= hi:
            raise HTTPException(400, f"{k} must be between {lo} and {hi}")
    with core.lock:
        return J(core.twin(shocks, user["username"]))


@api.get("/api/reverse")
def reverse(x: str = "withdrawal_mult", y: str = "deposit_delay_h", start: int | None = None,
            user: dict = Depends(need("analyst", "operator"))):
    from engine.stress import AXES
    if x not in AXES or y not in AXES or x == y:
        raise HTTPException(400, f"x and y must be two different axes from {', '.join(AXES)}")
    core = C()
    with core.lock:
        return J(core.reverse(x, y, None if start is None else T(start, core), user["username"]))


@api.get("/api/backtest")
def backtest(user: dict = Depends(ANY)):
    core = C()
    with core.lock:
        return J(core.backtest_view())


@api.get("/api/replay")
def replay(user: dict = Depends(ANY)):
    core = C()
    with core.lock:
        return J(core.replay_view())


@api.get("/api/trust")
def trust(user: dict = Depends(ANY)):
    out = C().trust()
    out["demo_tools"] = S.demo_tools
    return J(out)


@api.get("/api/policy")
def get_policy(user: dict = Depends(ANY)):
    return J(C().policy_rec)


@api.put("/api/policy")
def put_policy(body: dict = Body(...), user: dict = Depends(need("admin"))):
    try:
        pol = yaml.safe_load(body["yaml"]) if "yaml" in body else body["policy"]
        assert isinstance(pol, dict), "the policy must be a mapping"
        for section in ("reserve", "caps", "overdraft", "nightlock", "circuit_breaker", "approvals", "assumptions", "baseline"):
            assert section in pol, f"missing section '{section}'"
        assert 0.5 <= float(pol["reserve"]["quantile"]) < 1, "reserve.quantile must be between 0.5 and 1"
    except Exception as e:
        raise HTTPException(400, f"Invalid policy: {e}")
    core = C()
    with core.lock:
        rec = core.save_policy(pol, user["username"])
        core.replays(); core.backtest()
    return J(rec)


@api.put("/api/balances")
def put_balances(body: dict = Body(...), user: dict = Depends(need("admin"))):
    core = C()
    try:
        with core.lock:
            b = core.set_balances(body, user["username"])
            core.replays(); core.backtest()
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"Invalid balances: {e}")
    return J(b)


@api.post("/api/approvals")
def request_approval(body: dict = Body(...), user: dict = Depends(need("operator"))):
    try:
        amount = float(body["amount"])
        assert amount > 0 and math.isfinite(amount)
        kind = str(body["kind"])[:40]
    except Exception:
        raise HTTPException(400, "Send a kind and a positive amount")
    thr = C().policy["approvals"]["maker_checker_above_usd"]
    return J(store.request(kind, amount, body.get("detail", {}), user["username"], thr))


@api.post("/api/approvals/{aid}/decide")
def decide(aid: int, approve: bool = Body(..., embed=True), user: dict = Depends(need("approver"))):
    try:
        return J(store.decide(aid, user["username"], approve))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@api.get("/api/audit/verify")
def verify(user: dict = Depends(ANY)):
    return J(store.verify())


@api.post("/api/audit/tamper-test")
def tamper_test(user: dict = Depends(need("admin"))):
    """Demo: alter one record, verify (fails), restore it, verify again (passes)."""
    if not S.demo_tools:
        raise HTTPException(403, "Demo tools are switched off (RJ_DEMO_TOOLS=0)")
    entries = store.entries(5)
    if len(entries) < 3:
        raise HTTPException(400, "Need a few audit records first")
    seq = entries[2]["seq"]
    with store.lock:
        t = store.tamper(seq)
        broken = store.verify()
        store.restore(seq, t["original"])
    store.log("tamper_test", {"seq": seq, "by": user["username"]})
    return J({"tampered_seq": seq, "after_tamper": broken, "after_restore": store.verify()})


@api.get("/api/decisions/{seq}/reproduce")
def reproduce(seq: int, user: dict = Depends(ANY)):
    core = C()
    try:
        with core.lock:
            return J(core.reproduce(seq))
    except ValueError as e:
        raise HTTPException(400, str(e))


@api.post("/api/assistant")
def ask_assistant(body: dict = Body(...), user: dict = Depends(ANY)):
    """Built-in assistant: no external AI service, answers from the project's own engines."""
    limits.check(f"ask:{user['username']}", 60)
    q = str(body.get("question", "")).strip()[:1000]
    if not q:
        raise HTTPException(400, "Empty question")
    core = C()
    t = T(body.get("t"), core)
    session = f"{user['username']}:{str(body.get('session', 'default'))[:64]}"
    with core.lock:
        return J(engine.assistant.ask(q, t, session))


@api.post("/api/copilot")
def ask(body: dict = Body(...), user: dict = Depends(ANY)):
    limits.check(f"copilot:{user['username']}", 20)
    q = str(body.get("question", "")).strip()[:2000]
    if not q:
        raise HTTPException(400, "Empty question")
    C()
    return J(engine.copilot.ask(q))


@api.get("/api/report", response_class=HTMLResponse)
def report(user: dict = Depends(ANY)):
    core = C()
    b = core.backtest_view()
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rows = "".join(
        f"<tr><td>{m['label']}</td><td>{m['shortfall_hours']}</td><td>{m['net_return']/1e6:,.2f}</td>"
        f"<td>{m['overdraft_cost']/1e6:,.3f}</td><td>{m['haircut_cost']/1e6:,.2f}</td>"
        f"<td>{'' if m['capture'] is None else f'{m['capture']:.0%}'}</td></tr>" for m in b["metrics"])
    fc = "".join(f"<tr><td>{m['target']}</td><td>{m['h']} h</td><td>{m['model_wape']:.3f}</td>"
                 f"<td>{m['naive_wape']:.3f}</td></tr>" for m in core.fc.metrics)
    cov = core.fc.coverage[1]
    v = store.verify()
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Reserve Juggler report</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 16px;color:#1b1f24}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border-bottom:1px solid #e3e6ea;padding:6px 8px;text-align:left}}
th{{font-weight:600;background:#f6f7f9}}h1{{font-size:22px}}h2{{font-size:16px;margin-top:28px}}small{{color:#5b6470}}</style></head><body>
<h1>Reserve Juggler AI — summary report</h1><small>Dataset {esc(core.dataset['name'])} · sha256 {core.meta['sha256'][:16]} · policy v{core.policy_rec['version']}
 · model hgb-q50-conformal-v1 · generated by {esc(user['username'])}</small>
<h2>Policy comparison · {b['window'][0]} to {b['window'][1]}</h2>
<table><tr><th>Policy</th><th>Shortfall h</th><th>Net return $M</th><th>Overdraft $M</th><th>Haircuts $M</th><th>Capture</th></tr>{rows}</table>
<h2>Forecast accuracy (test window, WAPE)</h2><table><tr><th>Target</th><th>Horizon</th><th>Model</th><th>Seasonal naive</th></tr>{fc}</table>
<h2>Calibration (1 h net flow)</h2><p>95% band coverage {cov['static_95']:.1%} · 99% band {cov['static_99']:.1%} · ACI 95% {cov['aci_95']:.1%}</p>
<h2>Audit chain</h2><p>{'Verified' if v['ok'] else 'BROKEN at #' + str(v['broken_at'])} · {v['records']} records</p>
<p><small>Figures are modeled from the dataset above; starting balances are assumptions, not observations.</small></p></body></html>"""
    return HTMLResponse(html, headers={"Content-Disposition": 'attachment; filename="reserve-juggler-report.html"'})


# ---------- datasets (bring your own data) ----------
async def read_upload(request: Request) -> bytes:
    cap = S.max_upload_mb * 1024 * 1024
    if int(request.headers.get("content-length") or 0) > cap:
        raise HTTPException(413, f"File is larger than {S.max_upload_mb} MB")
    raw = await request.body()
    if len(raw) > cap:
        raise HTTPException(413, f"File is larger than {S.max_upload_mb} MB")
    return raw


@api.get("/api/datasets")
def list_datasets(user: dict = Depends(ANY)):
    core = engine.core
    return J({"datasets": registry.list(), "active": core.dataset["id"] if core else None, "engine": engine.status(),
              "fields": ds.FIELDS, "min_days": ds.MIN_DAYS, "max_upload_mb": S.max_upload_mb})


@api.post("/api/datasets/inspect")
async def inspect_dataset(request: Request):
    raw = await read_upload(request)       # read first, so a refused upload gets a clean 403 instead of a reset
    user = need("admin")(request)
    limits.check(f"upload:{user['username']}", 30)
    try:
        return J(ds.inspect(raw))
    except ds.DataError as e:
        raise HTTPException(400, str(e))


@api.post("/api/datasets")
async def upload_dataset(request: Request, name: str = "", source: str = "upload.csv", dayfirst: bool = True,
                         mapping: str = "{}"):
    raw = await read_upload(request)
    user = need("admin")(request)
    limits.check(f"upload:{user['username']}", 10)
    try:
        mp = json.loads(mapping)
        assert isinstance(mp, dict)
    except Exception:
        raise HTTPException(400, "mapping must be a JSON object of field → column")
    try:
        df, rep = ds.normalize(raw, mp, dayfirst)
    except ds.DataError as e:
        raise HTTPException(400, str(e))
    entry = registry.add_upload(df, rep, name, source, user["username"])
    store.log("dataset_uploaded", {"id": entry["id"], "name": entry["name"], "rows": entry["rows"], "by": user["username"],
                                   "assumed": entry["assumed"], "filled_hours": entry["filled_hours"]})
    return J({"dataset": entry, "report": rep})


def scaled_policy(pol: dict, factor: float) -> dict:
    import copy
    p = copy.deepcopy(pol)
    nice = lambda x: float(ds._round_nice(x * factor))
    for sec, key in (("reserve", "min_cash_usd"), ("reserve", "operational_buffer_usd"), ("reserve", "committed_payments_usd"),
                     ("baseline", "fixed_cash_floor_usd"), ("approvals", "maker_checker_above_usd")):
        if isinstance(p.get(sec, {}).get(key), (int, float)):
            p[sec][key] = nice(p[sec][key])
    return p


@api.post("/api/datasets/{did}/activate")
def activate_dataset(did: str, body: dict = Body(default={}), user: dict = Depends(need("admin"))):
    entry = registry.get(did)
    if not entry:
        raise HTTPException(404, "No such dataset")
    if body.get("balances"):
        try:
            b = {k: float(body["balances"][k]) for k in ("cash", "bonds", "equities")}
            assert all(v >= 0 and math.isfinite(v) for v in b.values())
        except Exception:
            raise HTTPException(400, "balances need non-negative cash, bonds and equities")
        store.set_kv(f"balances:{did}", b)
    if body.get("scale_policy") and did != "demo":
        import pandas as pd
        w = float(pd.read_csv(registry.file(did), usecols=["withdrawals_usd"])["withdrawals_usd"].mean())
        factor = w / 10_430_810.0           # the default policy was written for the demo file's average withdrawals
        base = DEFAULT_POLICY if body.get("scale_from_default", True) else store.current_policy()["policy"]
        store.save_policy(scaled_policy(base, factor), user["username"])
    store.log("dataset_activation_requested", {"id": did, "by": user["username"]})
    engine.request(did, refit=True, reason="activate")
    return J({"ok": True, "engine": engine.status()})


@api.delete("/api/datasets/{did}")
def delete_dataset(did: str, user: dict = Depends(need("admin"))):
    core = engine.core
    if core and core.dataset["id"] == did:
        raise HTTPException(400, "Activate another dataset before deleting this one")
    try:
        registry.delete(did)
    except ds.DataError as e:
        raise HTTPException(400, str(e))
    store.log("dataset_deleted", {"id": did, "by": user["username"]})
    return J({"ok": True})


# ---------- live mode ----------
@api.post("/api/live/rows")
async def live_rows(request: Request):
    raw = await read_upload(request)
    user = need("ingest")(request)
    limits.check(f"ingest:{user['username']}", 600)
    core = C()
    try:
        if request.headers.get("content-type", "").startswith("text/csv"):
            import io
            import pandas as pd
            payload = pd.read_csv(io.BytesIO(raw)).to_dict("records")
        else:
            body = json.loads(raw or b"{}")
            payload = body.get("rows") if isinstance(body, dict) else body
        tail = core.df.iloc[-1].to_dict()
        rows = ds.live_rows(payload, tail)
        res = registry.append(core.dataset["id"], rows)
    except ds.DataError as e:
        raise HTTPException(400 if "after the last" not in str(e) else 409, str(e))
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"Could not read the rows: {e}")
    store.log("live_rows", {"dataset": core.dataset["id"], "added": res["added"], "last": res["last"],
                            "gap_filled": res["gap_filled"], "by": user["username"]})
    engine.after_ingest()
    return J({**res, "engine": engine.status()})


@api.get("/api/live/status")
def live_status(user: dict = Depends(ANY)):
    return J(engine.status())


# ---------- public landing-page endpoints (RJ_PUBLIC_DEMO) ----------
def public_only(request: Request):
    if not S.public_demo:
        raise HTTPException(404, "Not found")


@api.get("/api/public/headline", dependencies=[Depends(public_only)])
def public_headline():
    core = C()
    with core.lock:
        o = core.overview(core.default_t)
        bt = {m["policy"]: m for m in core.backtest_view()["metrics"]}
    net = next(m for m in core.fc.metrics if m["target"] == "net" and m["h"] == 1)
    return J({"ts": o["ts"], "status": o["status"], "next_action": o["next_action"], "cash": o["state"]["cash"],
              "reserve": o["reserve"], "breaker": o["breaker"]["on"], "dataset": core.dataset["name"], "live": core.live,
              "error_reduction": 1 - net["model_wape"] / net["naive_wape"], "coverage": core.fc.coverage[1]["static_95"],
              "gain_vs_fixed": bt["optimized"]["net_return"] - bt["fixed"]["net_return"],
              "gap_hours": bt["optimized"]["shortfall_hours"]})


@api.post("/api/public/ask", dependencies=[Depends(public_only)])
def public_ask(request: Request, body: dict = Body(...)):
    limits.check(f"public-ask:{client_ip(request)}", 12)
    q = str(body.get("question", "")).strip()[:500]
    if not q:
        raise HTTPException(400, "Empty question")
    core = C()
    with core.lock:
        return J(engine.assistant.ask(q, core.default_t, f"public:{str(body.get('session', ''))[:32]}"))


# ---------- pages ----------
FRONT = settings_mod.ROOT / "frontend"


NO_CACHE = {"Cache-Control": "no-cache"}     # pages always re-check, so new ?v= asset links are picked up


@api.get("/", include_in_schema=False)
def landing():
    return FileResponse(FRONT / "landing.html", headers=NO_CACHE)


@api.get("/app", include_in_schema=False)
def dashboard():
    return FileResponse(FRONT / "index.html", headers=NO_CACHE)


api.mount("/", StaticFiles(directory=FRONT, html=True), name="frontend")

if __name__ == "__main__":
    log.info("Reserve Juggler on http://%s:%d (data in %s)", S.host, S.port, S.data_dir)
    if auth.needs_setup():
        log.info("No accounts yet: open the app to create the first admin, or run python backend/manage.py create-user")
    uvicorn.run(api, host=S.host, port=S.port, log_level="warning", proxy_headers=bool(S.trusted_proxies),
                forwarded_allow_ips=",".join(S.trusted_proxies) or None)
