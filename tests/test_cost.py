"""Rental cost: price table, per-GPU prices, a service's rent and the cost of a million tokens."""
import json
import os
import shutil
import tempfile
import unittest

from farsight import cost

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "prices.example.json")


def book(settings=None, table=EXAMPLE):
    return cost.PriceBook(cost.load_prices(table), cost.clean_settings(settings or {}))


class Prices(unittest.TestCase):
    def test_without_a_file_every_price_comes_from_the_user(self):
        table = cost.load_prices()
        self.assertEqual((table["gpus"], table["alpha"], table["currency"]), ([], 0.1, "$"))
        self.assertEqual(book(table=None).price({"name": "NVIDIA H100 80GB HBM3", "mem_total": 81559}), (None, None, None))
        self.assertEqual(book({"overrides": {"NVIDIA H100 80GB HBM3": 2.4}}, None).price({"name": "NVIDIA H100 80GB HBM3", "mem_total": 81559}),
                         (2.4, None, "override"))

    def test_example_table(self):
        table = cost.load_prices(EXAMPLE)
        self.assertGreaterEqual(len(table["gpus"]), 4)
        self.assertEqual((table["alpha"], table["currency"]), (0.1, "$"))
        for entry in table["gpus"]:
            self.assertTrue(entry["prices"]["low"] <= entry["prices"]["mid"] <= entry["prices"]["high"], entry["model"])

    def test_matching_order(self):
        prices = book()
        self.assertEqual(prices.price({"name": "NVIDIA H200", "mem_total": 143771})[:2], (3.5, "NVIDIA H200 141GB"))
        # two models share a name: the memory size decides
        self.assertEqual(prices.price({"name": "NVIDIA A100-SXM4-80GB", "mem_total": 81920})[:2], (1.5, "NVIDIA A100 80GB"))
        self.assertEqual(prices.price({"name": "NVIDIA A100-SXM4-40GB", "mem_total": 40960})[:2], (1.1, "NVIDIA A100 40GB"))
        # the PCI id decides when the driver reports a name that says nothing
        self.assertEqual(prices.price({"name": "NVIDIA Graphics Device", "mem_total": 81920, "device": "0x20B210DE"})[:2], (1.5, "NVIDIA A100 80GB"))
        self.assertEqual(prices.price({"name": "NVIDIA Graphics Device", "mem_total": 81920, "device": "0x20B510DE"})[1], "NVIDIA A100 80GB")
        self.assertEqual(prices.price({"name": "Unknown 9000", "mem_total": 46068})[1], "NVIDIA L40S 48GB")     # by memory size alone
        self.assertEqual(prices.price({"name": "G200", "mem_total": 196608}), (None, None, None))

    def test_tier_and_user_price(self):
        self.assertEqual(book({"tier": "high"}).price({"name": "NVIDIA H200", "mem_total": 143771})[0], 4.2)
        self.assertEqual(book({"overrides": {"NVIDIA H200": 5.5}}).price({"name": "NVIDIA H200", "mem_total": 143771}),
                         (5.5, "NVIDIA H200 141GB", "override"))
        self.assertEqual(book({"overrides": {"G200": 4.0}}).price({"name": "G200", "mem_total": 196608}), (4.0, None, "override"))

    def test_settings_are_validated(self):
        self.assertEqual(cost.clean_settings({"tier": "low", "alpha": 0.05, "currency": " $ ", "overrides": {"X": 3, "Y": None}}),
                         {"tier": "low", "alpha": 0.05, "currency": "$", "overrides": {"X": 3.0}})
        for bad in ({"tier": "cheap"}, {"alpha": 1.5}, {"alpha": True}, {"currency": "x" * 9}, {"overrides": {"X": -1}},
                    {"overrides": {"X": float("nan")}}, {"overrides": {"": 1}}, {"overrides": [1]}, "cost"):
            with self.assertRaises(cost.SettingsError):
                cost.clean_settings(bad)
        # a later change keeps what it does not mention
        self.assertEqual(cost.clean_settings({"alpha": 0.2}, {"tier": "high", "overrides": {"X": 1.0}})["tier"], "high")

    def test_price_files(self):
        directory = tempfile.mkdtemp()
        try:
            path = os.path.join(directory, "prices.json")
            # extra keys are ignored, entries without prices are skipped, a table priced in yuan says so by its key
            with open(path, "w") as handle:
                json.dump({"cache_hit_cost_ratio": 0.05, "note": "ignored", "gpus": [
                    {"model": "Card A", "match": {"name_regex": "Card A", "pci_device_ids": [], "memory_gib_range": [20, 28]},
                     "memory_gb": 24, "price_cny_per_gpu_hour": {"low": 1, "mid": 2, "high": 3}, "sources": ["somewhere"]},
                    {"model": "broken"}, "not an entry"]}, handle)
            table = cost.load_prices(path)
            self.assertEqual((len(table["gpus"]), table["alpha"], table["currency"]), (1, 0.05, u"\u00a5"))
            self.assertEqual(cost.PriceBook(table).price({"name": "Card A", "mem_total": 24564})[0], 2.0)
            with open(path, "w") as handle:
                json.dump({"currency": "EUR", "gpus": [{"model": "Card B", "match": {"name_regex": "Card B"}, "price_per_gpu_hour": {"mid": 4}}]}, handle)
            table = cost.load_prices(path)
            self.assertEqual((table["currency"], table["alpha"]), ("EUR", 0.1))
            self.assertEqual(cost.PriceBook(table, {"tier": "high"}).price({"name": "Card B", "mem_total": 1024})[0], 4.0)     # no "high": the middle tier
            with open(path, "w") as handle:
                handle.write("[]")
            with self.assertRaises(ValueError):
                cost.load_prices(path)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class Rent(unittest.TestCase):
    INVENTORY = {"p": [{"index": index, "name": "NVIDIA H100 80GB HBM3", "mem_total": 81559.0} for index in range(8)],
                 "d": [{"index": index, "name": "NVIDIA H200", "mem_total": 143771.0} for index in range(4)]
                      + [{"index": index, "name": "G200", "mem_total": 196608.0} for index in range(4, 8)]}

    def components(self):
        return [{"role": "frontend", "node": "d", "gpus": []}, {"role": "prefill", "node": "p", "gpus": [0, 1, 2, 3]},
                {"role": "prefill", "node": "p", "gpus": [4, 5, 6, 7]}, {"role": "decode", "node": "d", "gpus": [0, 1, 2, 3]}]

    def test_rent_by_role(self):
        rent = cost.service_rent(self.components(), self.INVENTORY, book())
        self.assertEqual((rent["prefill"], rent["decode"], rent["other"], rent["total"]), (20.0, 14.0, 0.0, 34.0))
        self.assertEqual((rent["gpus"], rent["unpriced"]), (12, 0))
        self.assertEqual([(row["role"], row["name"], row["count"], row["unit"], row["amount"]) for row in rent["items"]],
                         [("prefill", "NVIDIA H100 80GB HBM3", 8, 2.5, 20.0), ("decode", "NVIDIA H200", 4, 3.5, 14.0)])

    def test_cards_without_a_price_are_counted_but_not_charged(self):
        parts = [{"role": "worker", "node": "d", "gpus": [2, 3, 4, 5]}]
        rent = cost.service_rent(parts, self.INVENTORY, book())
        self.assertEqual((rent["total"], rent["other"], rent["gpus"], rent["unpriced"]), (7.0, 7.0, 4, 2))
        priced = cost.service_rent(parts, self.INVENTORY, book({"overrides": {"G200": 4.0}}))
        self.assertEqual((priced["total"], priced["unpriced"]), (15.0, 0))
        self.assertEqual([row["source"] for row in priced["items"]], ["override", "table"])
        nothing = cost.service_rent(parts, self.INVENTORY, book(table=None))
        self.assertEqual((nothing["total"], nothing["gpus"], nothing["unpriced"]), (0.0, 4, 4))


class Figures(unittest.TestCase):
    RENT = {"total": 34.0, "prefill": 20.0, "decode": 14.0, "other": 0.0}

    def test_definitions_of_a_split_service(self):
        numbers = cost.figures(self.RENT, computed=14360.0, hit=251200.0, output=1990.0, alpha=0.1, mode="disagg", busy=0.5)
        self.assertTrue(numbers["split"])
        self.assertAlmostEqual(numbers["miss"], 20.0 / ((14360 + 0.1 * 251200) * 0.0036), places=6)
        self.assertAlmostEqual(numbers["hit"], 0.1 * numbers["miss"], places=9)
        self.assertAlmostEqual(numbers["output"], 14.0 / (1990 * 0.0036), places=6)
        self.assertAlmostEqual(numbers["blended"], 34.0 / ((14360 + 251200 + 1990) * 0.0036), places=6)
        self.assertAlmostEqual(numbers["idle_rent"], 17.0)
        # the two input prices together charge exactly the prefill rent: nothing is counted twice
        charged = (numbers["miss"] * 14360 + numbers["hit"] * 251200) * 0.0036
        self.assertAlmostEqual(charged, 20.0, places=6)

    def test_aggregated_service_has_no_split(self):
        rent = {"total": 66.0, "prefill": 0.0, "decode": 0.0, "other": 66.0}
        numbers = cost.figures(rent, computed=1440.0, hit=960.0, output=400.0, alpha=0.1, mode="agg", busy=None)
        self.assertFalse(numbers["split"])
        self.assertIsNone(numbers["miss"])
        self.assertAlmostEqual(numbers["output"], 66.0 / (400 * 0.0036))
        self.assertAlmostEqual(numbers["blended"], 66.0 / (2800 * 0.0036))
        self.assertIsNone(numbers["idle_rent"])

    def test_no_throughput_or_no_rent_gives_no_figure(self):
        self.assertIsNone(cost.figures(self.RENT, 0.0, 0.0, 0.0, 0.1, "disagg", 0.0)["blended"])
        self.assertIsNone(cost.figures(self.RENT, None, None, None, 0.1, "disagg")["output"])
        self.assertIsNone(cost.per_million(0.0, 1000.0))
        self.assertAlmostEqual(cost.per_million(36.0, 1000.0), 10.0)


if __name__ == "__main__":
    unittest.main()
