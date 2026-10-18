"""Admin command line for accounts and tokens. Works on the same data folder as the server (RJ_DATA_DIR).

    python backend/manage.py create-user alice --roles admin
    python backend/manage.py set-password alice
    python backend/manage.py create-token feeder --label "bank feed"
    python backend/manage.py list-users
    python backend/manage.py seed-demo          # one account per role, random passwords, for demos
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import settings as settings_mod  # noqa: E402

settings_mod.load_env()

from engine.audit import Store  # noqa: E402
from engine.auth import ROLES, Auth, AuthError  # noqa: E402


def ask_password(username: str) -> str:
    pw = getpass.getpass(f"Password for {username}: ")
    if pw != getpass.getpass("Repeat it: "):
        raise SystemExit("The passwords don't match")
    return pw


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user"); c.add_argument("username"); c.add_argument("--roles", default="viewer")
    p = sub.add_parser("set-password"); p.add_argument("username")
    t = sub.add_parser("create-token"); t.add_argument("username"); t.add_argument("--label", default="token")
    sub.add_parser("list-users")
    sub.add_parser("seed-demo")
    a = ap.parse_args()

    S = settings_mod.load()
    S.data_dir.mkdir(parents=True, exist_ok=True)
    auth = Auth(Store(S.db_path), S.session_hours)
    try:
        if a.cmd == "create-user":
            u = auth.create_user(a.username, ask_password(a.username), a.roles.split(","), "cli")
            print(f"Created {u['username']} with roles {', '.join(u['roles'])}")
        elif a.cmd == "set-password":
            auth.update_user(a.username, "cli", password=ask_password(a.username))
            print("Password changed; existing sessions were signed out")
        elif a.cmd == "create-token":
            r = auth.create_token(a.username, a.label, "cli")
            print(f"Token for {r['username']} ({r['label']}). Copy it now; it is not stored in readable form:\n{r['token']}")
        elif a.cmd == "list-users":
            for u in auth.users():
                print(f"{u['username']:20s} {'active' if u['active'] else 'disabled':9s} {','.join(u['roles'])}")
        elif a.cmd == "seed-demo":
            out = S.data_dir / "demo_credentials.txt"
            lines = []
            for name, roles in (("admin", ["admin"]), ("operator", ["operator"]), ("approver", ["approver"]),
                                ("analyst", ["analyst"]), ("viewer", ["viewer"]), ("feeder", ["ingest"])):
                if auth.user(name):
                    lines.append(f"{name:10s} (exists, password unchanged)")
                    continue
                pw = secrets.token_urlsafe(12)
                auth.create_user(name, pw, roles, "seed-demo")
                lines.append(f"{name:10s} {pw}   roles: {','.join(roles)}")
            tok = auth.create_token("feeder", "demo feed", "seed-demo")["token"] if auth.user("feeder") else ""
            text = "Demo accounts (change or disable them before real use)\n" + "\n".join(lines) + \
                   (f"\n\nFeed token for tools/live_feed.py (ingest only; uploading history needs an admin token):\n{tok}\n" if tok else "\n")
            out.write_text(text, encoding="utf-8")
            print(text + f"\nSaved to {out} (git-ignored). Delete it once you've noted the passwords.")
        print(f"Roles available: {', '.join(ROLES)}")
    except AuthError as e:
        raise SystemExit(f"Error: {e}")


if __name__ == "__main__":
    main()
