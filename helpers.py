"""Shared fixtures: small datasets cut from the demo file, and a real server process on a free port."""
from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "treasury_4tier_dataset.csv"
PY = sys.executable


def demo_slice(days: int, start_day: int = 0) -> pd.DataFrame:
    df = pd.read_csv(DEMO)
    return df.iloc[start_day * 24:(start_day + days) * 24].reset_index(drop=True)


def transactions_csv(days: int = 70) -> bytes:
    """A bank-export-like file: 15-minute rows, other column names, ISO dates, amounts with thousands separators."""
    df = demo_slice(days)
    ts = pd.to_datetime(df["timestamp"], format="%d-%m-%Y %H:%M")
    rows = []
    for t, d, w in zip(ts, df["deposits_usd"], df["withdrawals_usd"]):
        for q in range(4):
            rows.append({"Posted At": (t + pd.Timedelta(minutes=15 * q)).strftime("%Y-%m-%d %H:%M:%S"),
                         "Inflow": f"{d / 4:,.2f}", "Outflow": f"{w / 4:,.2f}"})
    out = pd.DataFrame(rows)
    out = out.drop(index=list(range(400, 440)))          # an outage: ten hours missing
    return out.to_csv(index=False).encode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """Minimal JSON client with its own cookie jar (one per signed-in user)."""

    def __init__(self, base: str, token: str | None = None):
        self.base, self.token = base, token
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body=None, headers: dict | None = None, csrf: bool = True, raw: bool = False):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if csrf:
            req.add_header("X-Requested-With", "rj")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=300) as r:
                content = r.read()
                return r.status, (content if raw else json.loads(content or b"null")), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            content = e.read()
            try:
                content = json.loads(content)
            except Exception:
                pass
            return e.code, content, {k.lower(): v for k, v in e.headers.items()}

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)


class Server:
    def __init__(self, days: int = 100, extra_env: dict | None = None):
        self.tmp = tempfile.TemporaryDirectory(prefix="rj-test-")
        d = Path(self.tmp.name)
        demo = d / "demo.csv"
        demo_slice(days).to_csv(demo, index=False)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {**os.environ, "RJ_DATA_DIR": str(d / "data"), "RJ_DEMO_CSV": str(demo), "RJ_PORT": str(self.port),
               "RJ_HOST": "127.0.0.1", "RJ_LOG_LEVEL": "WARNING", "ENABLE_CLAUDE_COPILOT": "0", **(extra_env or {})}
        self.log = open(d / "server.log", "w")
        self.proc = subprocess.Popen([PY, str(ROOT / "backend" / "app.py")], cwd=ROOT, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT)

    def wait_ready(self, timeout: float = 240):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited:\n" + self.logs())
            try:
                with urllib.request.urlopen(self.base + "/readyz", timeout=3) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.5)
        raise TimeoutError("server not ready:\n" + self.logs())

    def logs(self) -> str:
        self.log.flush()
        return (Path(self.tmp.name) / "server.log").read_text(errors="replace")[-4000:]

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        try:
            self.tmp.cleanup()
        except Exception:
            pass
