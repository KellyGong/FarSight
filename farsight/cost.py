"""Rental cost of the GPUs a service occupies, and the cost of a million tokens at the measured throughput.

  rent             sum of the hourly price of the GPUs a role occupies; it does not depend on the load
  prefill, miss    prefill rent / (computed tokens + alpha * cache-hit tokens)       per million tokens
  prefill, hit     alpha * (prefill, miss)        alpha is a pricing convention (default 0.1), not a measurement
  decode           decode rent / output tokens
  blended          total rent / all tokens (computed + hit + output)
  idle rent        rent * (1 - GPU busy share): the part of the rent that bought no tokens

All of the above charge the whole rent, idle time included. Prices come from the user: a price entered for a GPU
model on the Settings page, or else a price table given with --prices (see examples/prices.example.json).
"""
import json
import math
import re

TIERS = ("low", "mid", "high")
DEFAULT_SETTINGS = {"tier": "mid", "currency": None, "alpha": None, "overrides": {}}
DEFAULT_ALPHA = 0.1
MAX_PRICE = 100000.0


class SettingsError(ValueError):
    """A cost setting sent by the user is not acceptable; args[0] names the field."""


def parse_prices(table):
    """Price table from the document of a price file; raises ValueError when it is not one.

    An entry gives its three tiers under "price_per_gpu_hour"; "price_cny_per_gpu_hour" is read as well and makes
    the yuan sign the default currency.
    """
    if not isinstance(table, dict) or not isinstance(table.get("gpus"), list):
        raise ValueError("no \"gpus\" list")
    entries = []
    yuan = False
    for entry in table["gpus"]:
        if not isinstance(entry, dict) or not entry.get("model"):
            continue
        prices = entry.get("price_per_gpu_hour")
        if not isinstance(prices, dict):
            prices = entry.get("price_cny_per_gpu_hour")
            if not isinstance(prices, dict):
                continue
            yuan = True
        tiers = dict((tier, float(prices[tier])) for tier in TIERS if isinstance(prices.get(tier), (int, float)))
        if not tiers:
            continue
        match = entry.get("match") or {}
        entries.append({"model": str(entry["model"]), "memory_gb": entry.get("memory_gb"), "prices": tiers,
                        "name_regex": match.get("name_regex") or "", "pci": [str(item).lower() for item in match.get("pci_device_ids") or []],
                        "memory_range": match.get("memory_gib_range") or [0, 1e9]})
    ratio = table.get("cache_hit_cost_ratio")
    return {"gpus": entries, "alpha": float(ratio) if isinstance(ratio, (int, float)) else DEFAULT_ALPHA,
            "currency": str(table.get("currency") or (u"\u00a5" if yuan else "$"))[:8], "updated": table.get("updated")}


def load_prices(path=None):
    """Read a price file. Without one the table is empty: every price then comes from the user's settings."""
    if not path:
        return parse_prices({"gpus": []})
    with open(path) as handle:
        return parse_prices(json.load(handle))


def clean_settings(value, base=None):
    """Validated copy of user cost settings merged over `base`."""
    settings = dict(DEFAULT_SETTINGS, **(base or {}))
    settings["overrides"] = dict(settings.get("overrides") or {})
    if not isinstance(value, dict):
        raise SettingsError("cost")
    if "tier" in value:
        if value["tier"] not in TIERS:
            raise SettingsError("tier")
        settings["tier"] = value["tier"]
    if "currency" in value:
        currency = value["currency"]
        if currency is not None and (not isinstance(currency, str) or not 0 < len(currency.strip()) <= 8):
            raise SettingsError("currency")
        settings["currency"] = currency.strip() if currency else None
    if "alpha" in value:
        alpha = value["alpha"]
        if alpha is not None and (isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1):
            raise SettingsError("alpha")
        settings["alpha"] = alpha
    if "overrides" in value:
        overrides = value["overrides"]
        if not isinstance(overrides, dict) or len(overrides) > 200:
            raise SettingsError("overrides")
        cleaned = {}
        for name, price in overrides.items():
            if price is None:
                continue
            if (not isinstance(name, str) or not 0 < len(name) <= 80 or isinstance(price, bool)
                    or not isinstance(price, (int, float)) or not math.isfinite(price) or not 0 <= price <= MAX_PRICE):
                raise SettingsError("overrides")
            cleaned[name] = float(price)
        settings["overrides"] = cleaned
    return settings


class PriceBook(object):
    """The price table plus the user's settings: resolves the hourly price of one GPU."""

    def __init__(self, table, settings=None):
        self.table = table
        self.settings = dict(DEFAULT_SETTINGS, **(settings or {}))

    @property
    def alpha(self):
        value = self.settings.get("alpha")
        return self.table["alpha"] if value is None else value

    @property
    def currency(self):
        return self.settings.get("currency") or self.table["currency"]

    def match(self, gpu):
        """Table entry of a GPU {"name", "device" (PCI device id), "mem_total" (MiB)}: PCI id first, then name with
        memory size, then name alone, then memory size alone."""
        name = gpu.get("name") or ""
        device = str(gpu.get("device") or "").lower()
        memory = (gpu.get("mem_total") or 0) / 1024.0
        entries = self.table["gpus"]
        if device:
            for entry in entries:
                if any(device == item or device.startswith(item.replace("10de:", "0x")) for item in entry["pci"]):
                    return entry
        named = []
        for entry in entries:
            try:
                if entry["name_regex"] and re.search(entry["name_regex"], name):
                    named.append(entry)
            except re.error:
                continue
        for entry in named:
            if entry["memory_range"][0] <= memory <= entry["memory_range"][1]:
                return entry
        if named:
            return named[0]
        if memory:
            for entry in entries:
                if entry["memory_range"][0] <= memory <= entry["memory_range"][1] and entry["memory_range"][1] < 1e8:
                    return entry
        return None

    def price(self, gpu):
        """(price per hour or None, table model or None, "override" | "table" | None)."""
        entry = self.match(gpu)
        override = self.settings["overrides"].get(gpu.get("name") or "")
        if override is not None:
            return override, entry["model"] if entry else None, "override"
        if entry is None:
            return None, None, None
        tier = self.settings.get("tier") or "mid"
        price = entry["prices"].get(tier, entry["prices"].get("mid", next(iter(entry["prices"].values()))))
        return price, entry["model"], "table"


def service_rent(components, inventories, book):
    """Hourly rent of the GPUs the worker components occupy.

    `inventories` maps a node id to its GPU inventory. Returns {"total", "prefill", "decode", "other", "gpus",
    "unpriced", "items": [{"role", "name", "count", "unit", "amount", "source"}]}.
    """
    rows = {}
    rent = {"total": 0.0, "prefill": 0.0, "decode": 0.0, "other": 0.0, "gpus": 0, "unpriced": 0}
    for component in components:
        if component["role"] not in ("prefill", "decode", "worker", "server"):
            continue
        cards = dict((gpu["index"], gpu) for gpu in inventories.get(component["node"]) or [])
        share = component["role"] if component["role"] in ("prefill", "decode") else "other"
        for index in component.get("gpus") or []:
            gpu = cards.get(index) or {}
            price, model, source = book.price(gpu)
            rent["gpus"] += 1
            key = (share, gpu.get("name") or "GPU", price, source)
            row = rows.setdefault(key, {"role": share, "name": gpu.get("name") or "GPU", "count": 0, "unit": price, "amount": 0.0, "source": source})
            row["count"] += 1
            if price is None:
                rent["unpriced"] += 1
                continue
            row["amount"] += price
            rent[share] += price
            rent["total"] += price
    order = {"prefill": 0, "decode": 1, "other": 2}
    rent["items"] = sorted(rows.values(), key=lambda row: (order[row["role"]], row["name"]))
    for key in ("total", "prefill", "decode", "other"):
        rent[key] = round(rent[key], 4)
    for row in rent["items"]:
        row["amount"] = round(row["amount"], 4)
    return rent


def per_million(rent, tokens_per_second):
    """Cost of a million tokens when `rent` per hour buys `tokens_per_second`; None without rent or throughput."""
    if not rent or not tokens_per_second or tokens_per_second <= 0:
        return None
    return rent / (tokens_per_second * 0.0036)


def figures(rent, computed, hit, output, alpha, mode, busy=None):
    """Cost per million tokens from the rent (see service_rent) and token rates in tokens per second.

    `computed` / `hit` are input tokens the prefill computed / took from a cache, `output` generated tokens.
    `busy` is the GPU busy share (0..1) of the service's cards, or None.
    """
    tokens = (computed or 0.0) + (hit or 0.0) + (output or 0.0)
    result = {"blended": per_million(rent["total"], tokens), "miss": None, "hit": None, "output": None, "split": False,
              "idle_rent": None if busy is None else rent["total"] * (1.0 - max(0.0, min(1.0, busy))), "busy": busy}
    if mode == "disagg" and rent["prefill"] and rent["decode"]:
        result["split"] = True
        result["miss"] = per_million(rent["prefill"] + rent["other"], (computed or 0.0) + alpha * (hit or 0.0))
        result["hit"] = None if result["miss"] is None else alpha * result["miss"]
        result["output"] = per_million(rent["decode"], output)
    else:
        result["output"] = per_million(rent["total"], output)
    return result
