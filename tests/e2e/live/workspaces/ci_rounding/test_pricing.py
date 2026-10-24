import unittest

from pricing import format_yuan, line_total, order_total


class LineTotalTest(unittest.TestCase):
    def test_without_discount(self) -> None:
        self.assertEqual(line_total(250, 4, 0), 1000)

    def test_half_a_cent_rounds_up(self) -> None:
        self.assertEqual(line_total(5, 1, 50), 3)

    def test_half_a_cent_rounds_up_for_the_smallest_amount(self) -> None:
        self.assertEqual(line_total(1, 1, 50), 1)


class OrderTotalTest(unittest.TestCase):
    def test_each_line_is_rounded_before_summing(self) -> None:
        self.assertEqual(order_total([(5, 1, 50), (1, 1, 50), (250, 4, 0)]), 1004)


class FormatTest(unittest.TestCase):
    def test_two_decimals(self) -> None:
        self.assertEqual(format_yuan(1004), "10.04")


if __name__ == "__main__":
    unittest.main()
