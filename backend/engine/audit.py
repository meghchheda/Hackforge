"""Policy-as-code with maker-checker approvals (FR-23) and a hash-chained audit log (FR-12, FR-24)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

GENESIS = "0" * 64


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=float)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self.lock = threading.RLock()      # serialises multi-statement writes (e.g. the audit chain's next hash)
        self.db.executescript("""
        create table if not exists policies (version integer primary key, sha256 text, yaml text,
            created_by text, created_at text);
        create table if not exists approvals (id integer primary key autoincrement, kind text, amount real,
            detail text, requested_by text, decided_by text, status text, created_at text, decided_at text);
        create table if not exists audit_log (seq integer primary key, record text, prev_hash text, hash text);
        create table if not exists kv (key text primary key, value text);
        """)
        self.db.commit()

    @property
    def db(self) -> sqlite3.Connection:
        """One connection per thread: a single connection shared by the server's worker threads can return
        empty results when two requests use it at the same moment."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.execute("pragma journal_mode=wal")
            c.execute("pragma busy_timeout=30000")
            self._local.conn = c
            with self.lock:
                self._conns.append(c)
        return c

    def close(self):
        """Close every thread's connection (tests and shutdown)."""
        with self.lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
        self._local = threading.local()

    # ---------- small persistent settings (balances, active dataset) ----------
    def get_kv(self, key: str, default=None):
        row = self.db.execute("select value from kv where key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_kv(self, key: str, value):
        with self.lock:
            self.db.execute("insert into kv values (?,?) on conflict(key) do update set value=excluded.value",
                            (key, json.dumps(value)))
            self.db.commit()

    # ---------- policy ----------
    def current_policy(self) -> dict | None:
        row = self.db.execute("select version, sha256, yaml, created_by, created_at from policies "
                              "order by version desc limit 1").fetchone()
        if not row:
            return None
        return {"version": row[0], "sha256": row[1], "yaml": row[2], "created_by": row[3],
                "created_at": row[4], "policy": yaml.safe_load(row[2])}

    def policy_versions(self) -> list[dict]:
        rows = self.db.execute("select version, sha256, created_by, created_at from policies "
                               "order by version desc").fetchall()
        return [{"version": r[0], "sha256": r[1][:12], "created_by": r[2], "created_at": r[3]} for r in rows]

    def save_policy(self, policy: dict, user: str) -> dict:
        with self.lock:
            cur = self.current_policy()
            version = (cur["version"] + 1) if cur else 1
            policy = dict(policy, version=version)
            text = yaml.safe_dump(policy, sort_keys=False)
            sha = hashlib.sha256(text.encode()).hexdigest()
            self.db.execute("insert into policies values (?,?,?,?,?)", (version, sha, text, user, _now()))
            self.db.commit()
        self.log("policy_saved", {"version": version, "sha256": sha, "by": user})
        return self.current_policy()

    # ---------- approvals ----------
    def request(self, kind: str, amount: float, detail: dict, user: str, threshold: float) -> dict:
        status = "pending" if amount > threshold else "auto_approved"
        with self.lock:
            cur = self.db.execute(
                "insert into approvals (kind, amount, detail, requested_by, decided_by, status, created_at) "
                "values (?,?,?,?,?,?,?)",
                (kind, amount, canonical(detail), user, None if status == "pending" else "policy", status, _now()))
            self.db.commit()
            aid = cur.lastrowid
        self.log("approval_requested", {"id": aid, "kind": kind, "amount": amount, "by": user, "status": status})
        return self.approval(aid)

    def decide(self, aid: int, user: str, approve: bool) -> dict:
        a = self.approval(aid)
        if not a:
            raise ValueError("No such request")
        if a["status"] != "pending":
            raise ValueError(f"Request is already {a['status']}")
        if a["requested_by"] == user:
            raise PermissionError("Maker-checker: you cannot approve your own request")
        status = "approved" if approve else "rejected"
        with self.lock:
            self.db.execute("update approvals set status=?, decided_by=?, decided_at=? where id=?",
                            (status, user, _now(), aid))
            self.db.commit()
        self.log("approval_decided", {"id": aid, "status": status, "by": user})
        return self.approval(aid)

    def approval(self, aid: int) -> dict | None:
        r = self.db.execute("select id, kind, amount, detail, requested_by, decided_by, status, created_at, "
                            "decided_at from approvals where id=?", (aid,)).fetchone()
        return _approval(r) if r else None

    def approvals(self) -> list[dict]:
        rows = self.db.execute("select id, kind, amount, detail, requested_by, decided_by, status, created_at, "
                               "decided_at from approvals order by id desc limit 50").fetchall()
        return [_approval(r) for r in rows]

    # ---------- hash-chained audit log ----------
    def log(self, kind: str, payload: dict) -> dict:
        with self.lock:
            last = self.db.execute("select seq, hash from audit_log order by seq desc limit 1").fetchone()
            seq = (last[0] + 1) if last else 1
            prev = last[1] if last else GENESIS
            record = canonical({"seq": seq, "kind": kind, "at": _now(), "payload": payload})
            h = hashlib.sha256((prev + record).encode()).hexdigest()
            self.db.execute("insert into audit_log values (?,?,?,?)", (seq, record, prev, h))
            self.db.commit()
        return {"seq": seq, "hash": h}

    def entries(self, limit: int = 40) -> list[dict]:
        rows = self.db.execute("select seq, record, prev_hash, hash from audit_log order by seq desc limit ?",
                               (limit,)).fetchall()
        out = []
        for seq, rec, prev, h in rows:
            d = json.loads(rec)
            out.append({"seq": seq, "kind": d["kind"], "at": d["at"], "payload": d["payload"],
                        "hash": h[:12], "prev": prev[:12]})
        return out

    def get_entry(self, seq: int) -> dict | None:
        r = self.db.execute("select record from audit_log where seq=?", (seq,)).fetchone()
        return json.loads(r[0]) if r else None

    def verify(self) -> dict:
        prev = GENESIS
        n = 0
        for seq, rec, p, h in self.db.execute("select seq, record, prev_hash, hash from audit_log order by seq"):
            n += 1
            if p != prev or hashlib.sha256((prev + rec).encode()).hexdigest() != h:
                return {"ok": False, "records": n, "broken_at": seq}
            prev = h
        return {"ok": True, "records": n, "broken_at": None, "head": prev[:12]}

    def tamper(self, seq: int) -> dict:
        """Demo only: alter one stored record without re-hashing, so verification fails there."""
        r = self.db.execute("select record from audit_log where seq=?", (seq,)).fetchone()
        if not r:
            raise ValueError("No such record")
        d = json.loads(r[0])
        d["payload"]["tampered"] = True
        with self.lock:
            self.db.execute("update audit_log set record=? where seq=?", (canonical(d), seq))
            self.db.commit()
        return {"seq": seq, "original": r[0]}

    def restore(self, seq: int, original: str):
        with self.lock:
            self.db.execute("update audit_log set record=? where seq=?", (original, seq))
            self.db.commit()


def _approval(r) -> dict:
    return {"id": r[0], "kind": r[1], "amount": r[2], "detail": json.loads(r[3]), "requested_by": r[4],
            "decided_by": r[5], "status": r[6], "created_at": r[7], "decided_at": r[8]}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
