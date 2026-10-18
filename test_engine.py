"""Engine on a short dataset: generic splits, model cache, live projections. About a minute."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from engine import datasets as ds  # noqa: E402
from engine.audit import Store  # noqa: E402
from engine.state import App  # noqa: E402

from helpers import demo_slice  # noqa: E402


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.csv = d / "short.csv"
        df, rep = ds.normalize(demo_slice(90).to_csv(index=False).encode(), ds.suggest_mapping(list(demo_slice(1).columns)))
        cls.csv.write_bytes(ds.to_csv_bytes(df))
        cls.store = Store(d / "t.db")
        cls.cache = d / "cache"
        cls.app = App(cls.csv, cls.store, {"id": "short", "name": "short", "live": True, "assumed": []}, cls.cache)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_splits_are_generic(self):
        b = self.app.fc.bounds
        self.assertEqual(b["cal"][11:], "00:00")
        self.assertLess(b["cal"], b["test"])
        self.assertEqual(self.app.test_start, int(self.app.fc.idx["test"][0]))

    def test_ledger_invariant_and_backtest(self):
        for p, run in self.app.replays().items():
            self.assertLess(run["metrics"]["invariant_error"], 1.0, p)
        bt = self.app.backtest_view()
        self.assertEqual({m["policy"] for m in bt["metrics"]}, {"conservative", "fixed", "optimized", "oracle"})

    def test_cache_and_reuse(self):
        again = App(self.csv, self.store, {"id": "short", "name": "short", "live": True, "assumed": []}, self.cache)
        self.assertTrue(again.fc.cached)
        self.assertEqual([round(m["model_wape"], 9) for m in again.fc.metrics],
                         [round(m["model_wape"], 9) for m in self.app.fc.metrics])
        reused = App(self.csv, self.store, self.app.dataset, self.cache, prev=self.app)
        self.assertIs(reused.fc.models, self.app.fc.models)
        self.assertEqual(reused.fc.bounds, self.app.fc.bounds)

    def test_live_projection(self):
        a = self.app
        t = a.default_t
        self.assertEqual(t, len(a.df) - 1)                  # live datasets follow the newest hour
        o = a.overview(t)
        self.assertIn("Sweep", o["next_action"]["title"])
        nl = a.nightlock(t)
        self.assertTrue(nl["projected"])
        self.assertIsNone(nl["realized_peak_od"])
        tw = a.twin({"start": t - 5, "hours": 24, "withdrawal_mult": 2.0}, "tester")
        self.assertEqual(tw["projected_from"], 6)            # hours t-5 … t are observed, t+1 onward projected
        self.assertEqual(len(tw["ts"]), 72)

    def test_stale_feed_trips_breaker(self):
        a = self.app
        a.stale_reason = "Stale feed: test"
        try:
            o = a.overview(len(a.df) - 1)
            self.assertTrue(o["breaker"]["on"])
            self.assertEqual(o["next_action"]["title"], "Hold conservative mode")
        finally:
            a.stale_reason = None

    def test_balances_persist_per_dataset(self):
        b = self.app.set_balances({"cash": 5e8, "bonds": 1e8, "equities": 5e7}, "tester")
        self.assertEqual(self.store.get_kv("balances:short"), b)
        with self.assertRaises(ValueError):
            self.app.set_balances({"cash": -1, "bonds": 0, "equities": 0}, "tester")


if __name__ == "__main__":
    unittest.main()
