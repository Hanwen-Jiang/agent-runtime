"""Loading stock files and the stock rules."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Item:
    sku: str
    name: str
    qty: int
    reorder_at: int


def load(path: str | Path) -> list[Item]:
    """Read a CSV with the columns sku, name, qty, reorder_at."""
    with open(path, newline="", encoding="utf-8") as f:
        return [Item(row["sku"], row["name"], int(row["qty"]), int(row["reorder_at"])) for row in csv.DictReader(f)]


def total_units(items: list[Item]) -> int:
    return sum(i.qty for i in items)


def low_stock(items: list[Item]) -> list[Item]:
    """Items that are low on stock (see the README for the rule)."""
    return [i for i in items if i.qty < i.reorder_at]


def restock(items: list[Item], sku: str, amount: int) -> Item:
    for i in items:
        if i.sku == sku:
            i.qty += amount
            return i
    raise KeyError(sku)
