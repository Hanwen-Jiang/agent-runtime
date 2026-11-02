"""Command line: report and restock."""

from __future__ import annotations

import argparse
import csv

from inventory.core import load, low_stock, restock, total_units


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="inventory")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="print totals and the low-stock list")
    rep.add_argument("path")
    add = sub.add_parser("restock", help="add units to one item and save the file")
    add.add_argument("path")
    add.add_argument("sku")
    add.add_argument("amount", type=int)
    args = parser.parse_args(argv)
    items = load(args.path)
    if args.command == "report":
        print(f"{len(items)} items, {total_units(items)} units")
        for i in low_stock(items):
            print(f"LOW {i.sku} {i.name} {i.qty}/{i.reorder_at}")
        return
    item = restock(items, args.sku, args.amount)
    with open(args.path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sku", "name", "qty", "reorder_at"])
        for i in items:
            writer.writerow([i.sku, i.name, i.qty, i.reorder_at])
    print(f"{item.sku} now {item.qty}")
