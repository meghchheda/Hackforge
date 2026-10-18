import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from engine import datasets as ds  # noqa: E402
from engine.data import REQUIRED, load  # noqa: E402

from helpers import DEMO, demo_slice, transactions_csv  # noqa: E402


class Normalize(unittest.TestCase):
    def test_demo_file_maps_itself(self):
        raw = demo_slice(70).to_csv(index=False).encode()
        ins = ds.inspect(raw)
        self.assertEqual(ins["suggested"]["timestamp"], "timestamp")
        self.assertEqual(ins["suggested"]["deposits_usd"], "deposits_usd")
        self.assertTrue(ins["dayfirst_guess"])
        df, rep = ds.normalize(raw, ins["suggested"], dayfirst=True)
        self.assertEqual(list(df.columns), REQUIRED)
        self.assertEqual(rep["rows"], 70 * 24)
        self.assertEqual(rep["assumed"], [])
        self.assertEqual(rep["filled_hours"], 0)

    def test_transactions_are_bucketed_and_gaps_filled(self):
        raw = transactions_csv(70)
        ins = ds.inspect(raw)
        m = ins["suggested"]
        self.assertEqual((m["timestamp"], m["deposits_usd"], m["withdrawals_usd"]), ("Posted At", "Inflow", "Outflow"))
        df, rep = ds.normalize(raw, m, dayfirst=False)
        self.assertEqual(rep["rows"], 70 * 24)                 # continuous clock
        self.assertEqual(rep["filled_hours"], 10)              # the outage
        self.assertIn("tier1_repo_rate_apy", rep["assumed"])
        src = demo_slice(70)
        hourly = df.set_index("timestamp")["deposits_usd"]
        self.assertAlmostEqual(hourly.iloc[0], src["deposits_usd"].iloc[0], places=1)
        self.assertTrue((df["net_flow_usd"] == (df["deposits_usd"] - df["withdrawals_usd"]).round(2)).all())
        self.assertTrue(df.loc[df["timestamp"].dt.hour == 17, "repo_sweep_window_open"].all())

    def test_net_only(self):
        src = demo_slice(65)[["timestamp", "net_flow_usd"]]
        df, rep = ds.normalize(src.to_csv(index=False).encode(), {"timestamp": "timestamp", "net_flow_usd": "net_flow_usd"})
        self.assertTrue((df["deposits_usd"] >= 0).all() and (df["withdrawals_usd"] >= 0).all())
        self.assertTrue(any("net flow" in n.lower() for n in rep["notes"]))

    def test_rejections(self):
        short = demo_slice(20).to_csv(index=False).encode()
        with self.assertRaisesRegex(ds.DataError, "at least 60"):
            ds.normalize(short, ds.suggest_mapping(list(demo_slice(1).columns)))
        bad = demo_slice(70)
        bad.loc[5, "deposits_usd"] = -10
        bad.loc[5, "net_flow_usd"] = bad.loc[5, "deposits_usd"] - bad.loc[5, "withdrawals_usd"]
        with self.assertRaisesRegex(ds.DataError, "negative"):
            ds.normalize(bad.to_csv(index=False).encode(), ds.suggest_mapping(list(bad.columns)))
        mism = demo_slice(70)
        mism.loc[7, "net_flow_usd"] += 1000
        with self.assertRaisesRegex(ds.DataError, "Net flow"):
            ds.normalize(mism.to_csv(index=False).encode(), ds.suggest_mapping(list(mism.columns)))
        daily = demo_slice(200).iloc[::24]
        with self.assertRaisesRegex(ds.DataError, "hourly"):
            ds.normalize(daily.to_csv(index=False).encode(), ds.suggest_mapping(list(daily.columns)))
        with self.assertRaisesRegex(ds.DataError, "empty"):
            ds.read_csv(b"   ")

    def test_normalised_file_loads(self):
        df, rep = ds.normalize(transactions_csv(70), {"timestamp": "Posted At", "deposits_usd": "Inflow",
                                                      "withdrawals_usd": "Outflow"}, dayfirst=False)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            p.write_bytes(ds.to_csv_bytes(df))
            loaded, meta = load(p)
            self.assertTrue(all(c["ok"] for c in meta["checks"]), meta["checks"])
            self.assertEqual(len(loaded), 70 * 24)


class RegistryAppend(unittest.TestCase):
    def test_append_rules(self):
        with tempfile.TemporaryDirectory() as d:
            reg = ds.Registry(Path(d), DEMO)
            self.assertTrue(reg.get("demo")["readonly"])
            df, rep = ds.normalize(demo_slice(70).to_csv(index=False).encode(), ds.suggest_mapping(REQUIRED))
            e = reg.add_upload(df, rep, "t", "t.csv", "tester")
            tail = df.iloc[-1].to_dict()
            nxt = pd.Timestamp(df["timestamp"].iloc[-1]) + pd.Timedelta(hours=1)
            rows = ds.live_rows([{"timestamp": str(nxt), "deposits_usd": 10, "withdrawals_usd": 4},
                                 {"timestamp": str(nxt + pd.Timedelta(hours=3)), "deposits_usd": 1, "withdrawals_usd": 2}], tail)
            res = reg.append(e["id"], rows)
            self.assertEqual((res["added"], res["gap_filled"]), (4, 2))
            loaded, meta = load(reg.file(e["id"]))
            self.assertTrue(all(c["ok"] for c in meta["checks"]), meta["checks"])
            self.assertEqual(len(loaded), 70 * 24 + 4)
            self.assertEqual(float(loaded["tier1_repo_rate_apy"].iloc[-1]), float(tail["tier1_repo_rate_apy"]))
            with self.assertRaisesRegex(ds.DataError, "after the last"):
                reg.append(e["id"], rows)
            with self.assertRaisesRegex(ds.DataError, "read-only"):
                reg.append("demo", rows)
            with self.assertRaises(ds.DataError):
                ds.live_rows([{"timestamp": str(nxt), "deposits_usd": -1, "withdrawals_usd": 0}], tail)


if __name__ == "__main__":
    unittest.main()
