"""End to end against a real server process: sign-in, roles, maker-checker, uploads, live rows, stale feed."""
import time
import unittest
import urllib.parse

from helpers import Client, Server, transactions_csv

PW = "a-long-test-passphrase"


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # one data hour per wall-clock second, so the 2-hour stale limit is 2 s
        cls.srv = Server(days=100, extra_env={"RJ_LIVE_SECONDS_PER_HOUR": "1", "RJ_RETRAIN_EVERY_HOURS": "1000"})
        cls.srv.wait_ready()
        cls.base = cls.srv.base
        cls.admin = Client(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def login(self, user, pw=PW):
        c = Client(self.base)
        s, b, _ = c.post("/api/auth/login", {"username": user, "password": pw})
        self.assertEqual(s, 200, b)
        return c

    def wait_version(self, c, v0, dataset=None, timeout=240):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s, st, _ = c.get("/api/live/status")
            if st["version"] != v0 and not st["building"] and (dataset is None or st["dataset"]["id"] == dataset):
                return st
            time.sleep(0.5)
        self.fail(f"engine did not rebuild: {st}\n{self.srv.logs()}")

    def test_01_signed_out(self):
        anon = Client(self.base)
        s, b, h = anon.get("/api/meta")
        self.assertEqual(s, 401)
        self.assertEqual(h.get("x-frame-options"), "DENY")
        self.assertIn("default-src 'self'", h.get("content-security-policy", ""))
        s, b, _ = anon.get("/api/auth/status")
        self.assertTrue(b["setup_needed"])
        s, b, _ = anon.get("/api/public/headline")
        self.assertEqual(s, 200)
        self.assertIn("gain_vs_fixed", b)
        s, b, _ = anon.get("/api/report", raw=True)
        self.assertEqual(s, 401)

    def test_02_first_admin(self):
        s, b, _ = self.admin.post("/api/auth/setup", {"username": "boss", "password": "short"})
        self.assertEqual(s, 400)
        s, b, h = self.admin.post("/api/auth/setup", {"username": "boss", "password": PW})
        self.assertEqual(s, 200, b)
        self.assertIn("httponly", h.get("set-cookie", "").lower())
        s, b, _ = Client(self.base).post("/api/auth/setup", {"username": "evil", "password": PW})
        self.assertEqual(s, 400)
        s, me, _ = self.admin.get("/api/auth/me")
        self.assertEqual(me["roles"], ["admin"])

    def test_03_csrf_header_required(self):
        s, b, _ = self.admin.post("/api/approvals", {"kind": "x", "amount": 1}, csrf=False)
        self.assertEqual(s, 403)

    def test_04_users_and_roles(self):
        for u, roles in (("olivia", ["operator"]), ("andre", ["approver"]), ("vera", ["viewer"]), ("lockme", ["viewer"]),
                         ("both", ["operator", "approver"]), ("feeder", ["ingest"]), ("ana", ["analyst"])):
            s, b, _ = self.admin.post("/api/users", {"username": u, "password": PW, "roles": roles})
            self.assertEqual(s, 200, b)
        vera = self.login("vera")
        self.assertEqual(vera.get("/api/overview")[0], 200)
        self.assertEqual(vera.post("/api/approvals", {"kind": "funding", "amount": 3e7})[0], 403)
        self.assertEqual(vera.call("PUT", "/api/policy", {"yaml": "x: 1"})[0], 403)
        self.assertEqual(vera.post("/api/scenario", {"withdrawal_mult": 2})[0], 403)
        self.assertEqual(vera.get("/api/users")[0], 403)
        self.assertEqual(self.login("ana").post("/api/scenario", {"withdrawal_mult": 2, "hours": 24})[0], 200)

    def test_05_maker_checker(self):
        olivia, andre, both = self.login("olivia"), self.login("andre"), self.login("both")
        s, req, _ = olivia.post("/api/approvals", {"kind": "funding", "amount": 3e7, "detail": {}})
        self.assertEqual((s, req["status"], req["requested_by"]), (200, "pending", "olivia"))
        self.assertEqual(olivia.post(f"/api/approvals/{req['id']}/decide", {"approve": True})[0], 403)   # no approver role
        s, mine, _ = both.post("/api/approvals", {"kind": "funding", "amount": 3e7})
        s, b, _ = both.post(f"/api/approvals/{mine['id']}/decide", {"approve": True})
        self.assertEqual(s, 403)
        self.assertIn("own request", b["detail"])
        s, b, _ = andre.post(f"/api/approvals/{req['id']}/decide", {"approve": True})
        self.assertEqual((s, b["status"], b["decided_by"]), (200, "approved", "andre"))
        s, small, _ = olivia.post("/api/approvals", {"kind": "funding", "amount": 1e6})
        self.assertEqual(small["status"], "auto_approved")

    def test_06_lockout(self):
        c = Client(self.base)
        codes = [c.post("/api/auth/login", {"username": "lockme", "password": "wrong-wrong-wrong"})[0] for _ in range(6)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)

    def test_07_views(self):
        olivia = self.login("olivia")
        s, meta, _ = olivia.get("/api/meta")
        t = meta["default_t"]
        for path in (f"/api/overview?t={t}", "/api/forecast?h=6", f"/api/ladder?t={t}&gap=20000000&duration=8",
                     f"/api/nightlock?t={t}", f"/api/allocation?t={t}", "/api/backtest", "/api/replay", "/api/trust",
                     "/api/data", "/api/datasets", "/api/live/status", "/api/audit/verify"):
            s, b, _ = olivia.get(path)
            self.assertEqual(s, 200, (path, b))
        self.assertEqual(olivia.get("/api/reverse")[0], 200)
        self.assertEqual(olivia.get("/api/report", raw=True)[0], 200)
        self.assertEqual(olivia.get("/api/forecast?h=5")[0], 400)
        self.assertEqual(olivia.get("/api/overview?t=999999")[0], 400)
        self.assertEqual(olivia.get("/api/ladder?gap=-5")[0], 400)
        s, b, _ = olivia.post("/api/assistant", {"question": "how much should we sweep tonight", "t": t})
        self.assertEqual((s, b["intent"]), (200, "sweep"))

    def test_08_upload_and_activate(self):
        raw = transactions_csv(70)
        s, ins, _ = self.admin.post("/api/datasets/inspect", raw, headers={"Content-Type": "text/csv"})
        self.assertEqual(s, 200, ins)
        q = urllib.parse.urlencode({"name": "Bank export", "source": "export.csv", "dayfirst": "false",
                                    "mapping": __import__("json").dumps(ins["suggested"])})
        self.assertEqual(self.login("olivia").post(f"/api/datasets?{q}", raw, headers={"Content-Type": "text/csv"})[0], 403)
        s, up, _ = self.admin.post(f"/api/datasets?{q}", raw, headers={"Content-Type": "text/csv"})
        self.assertEqual(s, 200, up)
        self.assertEqual(up["report"]["filled_hours"], 10)
        did = up["dataset"]["id"]
        v0 = self.admin.get("/api/live/status")[1]["version"]
        s, b, _ = self.admin.post(f"/api/datasets/{did}/activate", {"balances": {"cash": 2.4e9, "bonds": 1.2e9, "equities": 4e8},
                                                                     "scale_policy": True})
        self.assertEqual(s, 200, b)
        self.wait_version(self.admin, v0, did)
        s, meta, _ = self.admin.get("/api/meta")
        self.assertEqual(meta["source"]["id"], did)
        self.assertEqual(len(meta["ts"]), 70 * 24)
        self.assertGreater(meta["policy_version"], 1)        # amounts scaled into a new policy version
        self.__class__.did = did

    def test_09_live_rows(self):
        s, tok, _ = self.admin.post("/api/tokens", {"username": "feeder", "label": "test feed"})
        self.assertEqual(s, 200)
        feed = Client(self.base, token=tok["token"])
        s, meta, _ = self.admin.get("/api/meta")
        last = meta["ts"][-1]
        import pandas as pd
        nxt = [str(pd.Timestamp(last) + pd.Timedelta(hours=k))[:16] for k in (1, 2, 3)]
        v0 = self.admin.get("/api/live/status")[1]["version"]
        s, b, _ = feed.post("/api/live/rows", {"rows": [{"timestamp": x, "deposits_usd": 5e6, "withdrawals_usd": 4e6} for x in nxt]})
        self.assertEqual((s, b.get("added")), (200, 3), b)
        self.assertEqual(feed.post("/api/live/rows", {"rows": [{"timestamp": nxt[0], "deposits_usd": 1, "withdrawals_usd": 1}]})[0], 409)
        self.assertEqual(feed.post("/api/live/rows", {"rows": [{"timestamp": "garbage", "deposits_usd": 1}]})[0], 400)
        self.assertEqual(feed.get("/api/users")[0], 403)                    # ingest tokens can't do anything else
        st = self.wait_version(self.admin, v0)
        self.assertTrue(st["live"])
        s, meta, _ = self.admin.get("/api/meta")
        self.assertEqual(meta["ts"][-1], nxt[-1])
        self.assertEqual(meta["default_t"], len(meta["ts"]) - 1)
        s, o, _ = self.admin.get("/api/overview")
        self.assertTrue(o["latest"])
        self.__class__.feed, self.__class__.last = feed, nxt[-1]

    def test_10_stale_feed(self):
        time.sleep(3.5)                                    # > 2 data hours at 1 s per hour
        s, o, _ = self.admin.get("/api/overview")
        self.assertTrue(o["breaker"]["on"], o["breaker"])
        self.assertIn("Stale feed", o["breaker"]["reason"])
        import pandas as pd
        nxt = str(pd.Timestamp(self.last) + pd.Timedelta(hours=1))[:16]
        self.assertEqual(self.feed.post("/api/live/rows", {"rows": [{"timestamp": nxt, "deposits_usd": 5e6, "withdrawals_usd": 4e6}]})[0], 200)
        s, st, _ = self.admin.get("/api/live/status")
        self.assertFalse(st["stale"])

    def test_11_audit(self):
        s, v, _ = self.admin.get("/api/audit/verify")
        self.assertTrue(v["ok"])
        s, t, _ = self.admin.post("/api/audit/tamper-test")
        self.assertEqual(s, 200)
        self.assertFalse(t["after_tamper"]["ok"])
        self.assertTrue(t["after_restore"]["ok"])
        kinds = {e["kind"] for e in self.admin.get("/api/trust")[1]["audit"]}
        self.assertTrue({"live_rows", "tamper_test"} <= kinds, kinds)

    def test_12_logout(self):
        c = self.login("vera")
        self.assertEqual(c.post("/api/auth/logout")[0], 200)
        self.assertEqual(c.get("/api/meta")[0], 401)


if __name__ == "__main__":
    unittest.main()
