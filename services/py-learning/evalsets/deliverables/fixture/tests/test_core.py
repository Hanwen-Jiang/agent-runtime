import unittest
from pathlib import Path

from inventory.core import Item, load, low_stock, total_units

DATA = Path(__file__).resolve().parents[1] / "data" / "stock.csv"


class CoreTest(unittest.TestCase):
    def test_load_reads_every_row(self) -> None:
        self.assertEqual(len(load(DATA)), 5)

    def test_total_units(self) -> None:
        self.assertEqual(total_units(load(DATA)), 243)

    def test_low_stock_includes_items_below_the_reorder_level(self) -> None:
        self.assertEqual([i.sku for i in low_stock([Item("x", "x", 1, 5)])], ["x"])

    def test_low_stock_includes_items_at_the_reorder_level(self) -> None:
        self.assertEqual([i.sku for i in low_stock([Item("y", "y", 5, 5)])], ["y"])


if __name__ == "__main__":
    unittest.main()
