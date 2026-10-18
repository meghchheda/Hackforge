"""Stream hourly rows from a CSV into a running Reserve Juggler server, as a bank feed would.

Typical demo (one data hour every 10 seconds, starting mid-November of the demo year):

    python tools/live_feed.py --url http://127.0.0.1:8000 --token $RJ_TOKEN \
        --csv data/treasury_4tier_dataset.csv --start "2026-11-16 10:00" --every 10 --upload-history

--upload-history first uploads every row before --start as a new dataset and activates it (needs an admin token),
so the stream continues exactly where the history stops. Without it, rows go into the active dataset, which must
be an uploaded one that ends just before --start. Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

FIELDS = ["deposits_usd", "withdrawals_usd", "tier1_repo_rate_apy", "tier2_bond_yield_pct", "tier2_bond_price_index",
          "tier2_bond_mod_duration", "tier3_stock_index_price", "tier3_stock_volatility_pct",
          "tier1_early_redemption_haircut_pct", "tier2_bond_liquidation_haircut_pct",
          "tier3_stock_liquidation_haircut_pct", "emergency_overdraft_penalty_apr_pct"]


def parse_ts(s: str) -> datetime:
    for fmt in ("%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    raise SystemExit(f"Unreadable timestamp: {s!r}")


def call(base: str, token: str, path: str, body=None, method: str | None = None, ctype="application/json"):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        raise SystemExit(f"{method or 'POST' if data else 'GET'} {path} failed ({e.code}): {detail}")


def wait_for(base, token, dataset_id, v0):
    print("Waiting for the engine to train on the history", end="", flush=True)
    for _ in range(600):
        st = call(base, token, "/api/live/status")
        if st["version"] != v0 and not st["building"] and (st.get("dataset") or {}).get("id") == dataset_id:
            print(f" ready ({st['rows']} rows, last {st['last_ts']})")
            return
        print(".", end="", flush=True)
        time.sleep(2)
    raise SystemExit("\nThe engine did not become ready in time")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default=os.environ.get("RJ_TOKEN"), help="API token (or set RJ_TOKEN)")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--start", required=True, help="first hour to stream, e.g. '2026-11-16 10:00'")
    ap.add_argument("--every", type=float, default=10.0, help="seconds between posts")
    ap.add_argument("--batch", type=int, default=1, help="hours per post")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many hours (0 = until the file ends)")
    ap.add_argument("--upload-history", action="store_true", help="upload rows before --start as a new dataset first")
    a = ap.parse_args()
    if not a.token:
        raise SystemExit("Give --token or set RJ_TOKEN (create one under Trust → Users, or: python backend/manage.py create-token)")

    start = parse_ts(a.start)
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows or "timestamp" not in rows[0]:
        raise SystemExit("The CSV needs a timestamp column")
    for r in rows:
        r["_t"] = parse_ts(r["timestamp"])
    history = [r for r in rows if r["_t"] < start]
    future = [r for r in rows if r["_t"] >= start]
    if not future:
        raise SystemExit("No rows at or after --start")

    if a.upload_history:
        if not history:
            raise SystemExit("No rows before --start to upload")
        buf = io.StringIO()
        cols = [c for c in rows[0].keys() if c != "_t"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(history)
        mapping = {c: c for c in cols}
        q = urllib.parse.urlencode({"name": f"Live from {start:%d %b %Y %H:%M}", "source": os.path.basename(a.csv),
                                    "dayfirst": "true", "mapping": json.dumps(mapping)})
        print(f"Uploading {len(history):,} hours of history…")
        up = call(a.url, a.token, f"/api/datasets?{q}", buf.getvalue().encode(), ctype="text/csv")
        did = up["dataset"]["id"]
        v0 = call(a.url, a.token, "/api/live/status")["version"]
        call(a.url, a.token, f"/api/datasets/{did}/activate", {"scale_policy": False})
        wait_for(a.url, a.token, did, v0)

    total = len(future) if not a.limit else min(a.limit, len(future))
    print(f"Streaming {total:,} hours, {a.batch} per post, every {a.every:g} s. Ctrl+C stops (the breaker will flag a stale feed).")
    sent = 0
    try:
        while sent < total:
            chunk = future[sent:sent + min(a.batch, total - sent)]
            payload = [{"timestamp": r["_t"].strftime("%Y-%m-%d %H:%M"),
                        **{k: float(r[k]) for k in FIELDS if k in r and r[k] != ""}} for r in chunk]
            res = call(a.url, a.token, "/api/live/rows", {"rows": payload})
            sent += len(chunk)
            eng = res.get("engine", {})
            print(f"  {res['last']}  +{res['added']} h  ({sent}/{total})  engine: {'rebuilding' if eng.get('building') else 'ready'}",
                  flush=True)
            time.sleep(a.every)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
