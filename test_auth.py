import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from engine.audit import Store  # noqa: E402
from engine.auth import Auth, AuthError, can, hash_password, verify_password  # noqa: E402

PW = "correct-horse-battery"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.auth = Auth(self.store, session_hours=1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_hashing(self):
        h = hash_password(PW)
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertNotIn(PW, h)
        self.assertTrue(verify_password(PW, h))
        self.assertFalse(verify_password(PW + "x", h))
        self.assertNotEqual(h, hash_password(PW))          # salted

    def test_setup_once_and_rules(self):
        self.assertTrue(self.auth.needs_setup())
        with self.assertRaisesRegex(AuthError, "10"):
            self.auth.setup_first_admin("boss", "short")
        with self.assertRaisesRegex(AuthError, "username"):
            self.auth.setup_first_admin("boss", "boss-is-the-best")
        self.auth.setup_first_admin("boss", PW)
        with self.assertRaisesRegex(AuthError, "already"):
            self.auth.setup_first_admin("other", PW)
        with self.assertRaisesRegex(AuthError, "Unknown role"):
            self.auth.create_user("x1", PW, ["wizard"], "boss")
        with self.assertRaisesRegex(AuthError, "taken"):
            self.auth.create_user("BOSS", PW, ["viewer"], "boss")

    def test_sessions_tokens_and_lockout(self):
        self.auth.setup_first_admin("boss", PW)
        self.auth.create_user("vic", PW, ["viewer"], "boss")
        token, u = self.auth.login("vic", PW, "1.2.3.4")
        self.assertEqual(self.auth.resolve(token)["username"], "vic")
        stored = self.store.db.execute("select token_hash from sessions").fetchall()
        self.assertNotIn(token, [r[0] for r in stored])     # only the hash is stored
        self.auth.logout(token)
        self.assertIsNone(self.auth.resolve(token))
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.auth.login("vic", "wrong-password!", "9.9.9.9")
        with self.assertRaisesRegex(AuthError, "Too many"):
            self.auth.login("vic", PW, "9.9.9.9")
        self.auth.login("vic", PW, "1.2.3.4")              # other clients are not locked out
        api = self.auth.create_token("vic", "feed", "boss")
        self.assertEqual(self.auth.resolve(api["token"])["via"], "api")
        self.auth.update_user("vic", "boss", active=False)
        self.assertIsNone(self.auth.resolve(api["token"]))
        self.auth.update_user("vic", "boss", active=True)
        self.assertIsNone(self.auth.resolve(api["token"]))  # disabling revoked it for good
        api2 = self.auth.create_token("vic", "feed 2", "boss")
        self.auth.revoke_token(api2["id"], "boss")
        self.assertIsNone(self.auth.resolve(api2["token"]))

    def test_concurrent_session_checks(self):
        """Parallel requests with one cookie must all resolve (a shared SQLite connection used to drop some)."""
        import threading
        self.auth.setup_first_admin("boss", PW)
        token, _ = self.auth.login("boss", PW, "1.2.3.4")
        misses = []

        def hammer():
            for _ in range(150):
                if not self.auth.resolve(token):
                    misses.append(1)
                self.store.log("ping", {})
        threads = [threading.Thread(target=hammer) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(misses), 0)
        self.assertTrue(self.store.verify()["ok"])          # the audit chain stayed consistent under load

    def test_last_admin_is_protected(self):
        self.auth.setup_first_admin("boss", PW)
        with self.assertRaisesRegex(AuthError, "admin"):
            self.auth.update_user("boss", "boss", roles=["viewer"])
        with self.assertRaisesRegex(AuthError, "admin"):
            self.auth.update_user("boss", "boss", active=False)

    def test_can(self):
        self.assertTrue(can({"roles": ["admin"]}, "approver"))
        self.assertTrue(can({"roles": ["operator"]}, "analyst", "operator"))
        self.assertFalse(can({"roles": ["viewer"]}, "operator"))
        self.assertTrue(can({"roles": ["viewer"]}))
        self.assertFalse(can(None))

    def test_audit_records_logins(self):
        self.auth.setup_first_admin("boss", PW)
        with self.assertRaises(AuthError):
            self.auth.login("boss", "nope-nope-nope", "1.1.1.1")
        kinds = [e["kind"] for e in self.store.entries(10)]
        self.assertIn("login_failed", kinds)
        self.assertIn("user_created", kinds)
        self.assertTrue(self.store.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
