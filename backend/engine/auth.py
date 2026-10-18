"""Accounts, roles, sessions and API tokens, stored in the same SQLite file as the audit chain.

Passwords: PBKDF2-HMAC-SHA256 with a random salt. Session cookies and API tokens are random 256-bit values;
only their SHA-256 hash is stored, so a copy of the database does not let anyone sign in.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from .audit import Store

ROLES = ("admin", "operator", "approver", "analyst", "viewer", "ingest")
ROLE_HELP = {
    "admin": "Everything, including users, policy, balances and datasets",
    "operator": "Runs the desk: funding, sweeps and allocation; requests approvals",
    "approver": "Approves or rejects other people's requests",
    "analyst": "Runs stress tests and scenarios",
    "viewer": "Read-only",
    "ingest": "Machine role: may push live data rows",
}
ITERATIONS = 310_000
MIN_PASSWORD = 10
USERNAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


class AuthError(Exception):
    pass


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_username(name: str) -> str:
    n = (name or "").strip().lower()
    if not 2 <= len(n) <= 40 or not set(n) <= USERNAME_CHARS:
        raise AuthError("Usernames are 2–40 characters: letters, digits, dot, dash or underscore")
    return n


def check_password_strength(pw: str, username: str = ""):
    if len(pw or "") < MIN_PASSWORD:
        raise AuthError(f"Passwords need at least {MIN_PASSWORD} characters")
    if username and username.lower() in pw.lower():
        raise AuthError("The password must not contain the username")


def clean_roles(roles) -> list[str]:
    rs = sorted({str(r).strip().lower() for r in (roles or []) if str(r).strip()})
    bad = [r for r in rs if r not in ROLES]
    if bad:
        raise AuthError(f"Unknown role(s): {', '.join(bad)}")
    if not rs:
        raise AuthError("Give the user at least one role")
    return rs


class Auth:
    def __init__(self, store: Store, session_hours: int = 12):
        self.store = store
        self.session_hours = session_hours
        self._fails: dict[str, list[float]] = {}
        self._fail_lock = threading.Lock()
        with store.lock:
            self.db.executescript("""
            create table if not exists users (username text primary key, pw_hash text, roles text,
                active integer default 1, created_at text, created_by text, last_login text);
            create table if not exists sessions (token_hash text primary key, username text, kind text,
                label text, created_at text, expires_at text, last_used text);
            """)
            self.db.commit()

    @property
    def db(self):
        return self.store.db              # this thread's connection

    # ---------- users ----------
    def needs_setup(self) -> bool:
        return self.db.execute("select count(*) from users").fetchone()[0] == 0

    def create_user(self, username: str, password: str, roles, by: str) -> dict:
        u = clean_username(username)
        check_password_strength(password, u)
        rs = clean_roles(roles)
        with self.store.lock:
            if self.db.execute("select 1 from users where username=?", (u,)).fetchone():
                raise AuthError("That username is taken")
            self.db.execute("insert into users values (?,?,?,?,?,?,?)",
                            (u, hash_password(password), ",".join(rs), 1, _iso(_now()), by, None))
            self.db.commit()
        self.store.log("user_created", {"username": u, "roles": rs, "by": by})
        return self.user(u)

    def setup_first_admin(self, username: str, password: str) -> dict:
        with self.store.lock:          # one-time: only while no account exists
            if not self.needs_setup():
                raise AuthError("Setup is already complete")
            return self.create_user(username, password, ["admin"], "setup")

    def update_user(self, username: str, by: str, roles=None, active=None, password=None) -> dict:
        u = self.user(username)
        if not u:
            raise AuthError("No such user")
        changes = {}
        with self.store.lock:
            if roles is not None:
                rs = clean_roles(roles)
                if "admin" in u["roles"] and "admin" not in rs and self._admins() <= 1:
                    raise AuthError("Keep at least one active admin")
                self.db.execute("update users set roles=? where username=?", (",".join(rs), u["username"]))
                changes["roles"] = rs
            if active is not None:
                if not active and "admin" in u["roles"] and self._admins() <= 1:
                    raise AuthError("Keep at least one active admin")
                self.db.execute("update users set active=? where username=?", (1 if active else 0, u["username"]))
                changes["active"] = bool(active)
                if not active:
                    self.db.execute("delete from sessions where username=?", (u["username"],))
            if password is not None:
                check_password_strength(password, u["username"])
                self.db.execute("update users set pw_hash=? where username=?", (hash_password(password), u["username"]))
                self.db.execute("delete from sessions where username=? and kind='session'", (u["username"],))
                changes["password"] = "reset"
            self.db.commit()
        self.store.log("user_updated", {"username": u["username"], "changes": changes, "by": by})
        return self.user(u["username"])

    def _admins(self) -> int:
        return sum(1 for r in self.db.execute("select roles from users where active=1") if "admin" in r[0].split(","))

    def user(self, username: str) -> dict | None:
        r = self.db.execute("select username, roles, active, created_at, created_by, last_login from users "
                            "where username=?", ((username or "").strip().lower(),)).fetchone()
        return _user(r) if r else None

    def users(self) -> list[dict]:
        rows = self.db.execute("select username, roles, active, created_at, created_by, last_login from users "
                               "order by username").fetchall()
        return [_user(r) for r in rows]

    # ---------- sign-in with lockout ----------
    def login(self, username: str, password: str, client: str) -> tuple[str, dict]:
        key = f"{(username or '').strip().lower()}|{client}"
        with self._fail_lock:
            recent = [t for t in self._fails.get(key, []) if time.time() - t < 300]
            self._fails[key] = recent
            if len(recent) >= 5:
                raise AuthError("Too many failed attempts. Wait 5 minutes and try again.")
        row = self.db.execute("select pw_hash, active from users where username=?",
                              ((username or "").strip().lower(),)).fetchone()
        ok = bool(row) and row[1] == 1 and verify_password(password or "", row[0])
        if not row:
            verify_password(password or "", hash_password("timing-equaliser", b"0" * 16))   # same work either way
        if not ok:
            with self._fail_lock:
                self._fails.setdefault(key, []).append(time.time())
            self.store.log("login_failed", {"username": (username or "")[:40], "client": client})
            raise AuthError("Wrong username or password")
        with self._fail_lock:
            self._fails.pop(key, None)
        u = self.user(username)
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.store.lock:
            self.db.execute("insert into sessions values (?,?,?,?,?,?,?)",
                            (_token_hash(token), u["username"], "session", client[:60], _iso(now),
                             _iso(now + timedelta(hours=self.session_hours)), _iso(now)))
            self.db.execute("update users set last_login=? where username=?", (_iso(now), u["username"]))
            self.db.execute("delete from sessions where kind='session' and expires_at < ?", (_iso(now),))
            self.db.commit()
        self.store.log("login", {"username": u["username"], "client": client})
        return token, u

    def logout(self, token: str):
        with self.store.lock:
            self.db.execute("delete from sessions where token_hash=?", (_token_hash(token),))
            self.db.commit()

    def resolve(self, token: str | None) -> dict | None:
        """Session cookie or API token → active user with roles, or None."""
        if not token:
            return None
        h = _token_hash(token)
        r = self.db.execute("select username, kind, expires_at, label from sessions where token_hash=?", (h,)).fetchone()
        if not r:
            return None
        if r[2] and r[2] < _iso(_now()):
            self.logout(token)
            return None
        u = self.user(r[0])
        if not u or not u["active"]:
            return None
        if r[1] == "session":           # sliding expiry, written at most every few minutes
            new_exp = _iso(_now() + timedelta(hours=self.session_hours))
            if new_exp[:15] != r[2][:15]:
                with self.store.lock:
                    self.db.execute("update sessions set expires_at=?, last_used=? where token_hash=?",
                                    (new_exp, _iso(_now()), h))
                    self.db.commit()
        return {**u, "via": r[1], "token_label": r[3]}

    # ---------- API tokens for scripts and live feeds ----------
    def create_token(self, username: str, label: str, by: str) -> dict:
        u = self.user(username)
        if not u or not u["active"]:
            raise AuthError("No such active user")
        token = "rj_" + secrets.token_urlsafe(32)
        with self.store.lock:
            self.db.execute("insert into sessions values (?,?,?,?,?,?,?)",
                            (_token_hash(token), u["username"], "api", (label or "token")[:60], _iso(_now()), None, None))
            self.db.commit()
        self.store.log("token_created", {"username": u["username"], "label": label, "by": by})
        return {"token": token, "username": u["username"], "label": label, "id": _token_hash(token)[:12]}

    def tokens(self) -> list[dict]:
        rows = self.db.execute("select token_hash, username, label, created_at, last_used from sessions "
                               "where kind='api' order by created_at desc").fetchall()
        return [{"id": r[0][:12], "username": r[1], "label": r[2], "created_at": r[3], "last_used": r[4]} for r in rows]

    def revoke_token(self, token_id: str, by: str):
        with self.store.lock:
            n = self.db.execute("delete from sessions where kind='api' and substr(token_hash,1,12)=?",
                                (token_id,)).rowcount
            self.db.commit()
        if not n:
            raise AuthError("No such token")
        self.store.log("token_revoked", {"id": token_id, "by": by})


def _user(r) -> dict:
    return {"username": r[0], "roles": r[1].split(",") if r[1] else [], "active": bool(r[2]),
            "created_at": r[3], "created_by": r[4], "last_login": r[5]}


def can(user: dict | None, *roles: str) -> bool:
    """Admins can do everything; otherwise the user needs one of the listed roles."""
    if not user:
        return False
    have = set(user["roles"])
    return "admin" in have or bool(have & set(roles)) or not roles
