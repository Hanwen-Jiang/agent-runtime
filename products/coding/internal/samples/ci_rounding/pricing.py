"""订单金额。金额一律以分为单位的整数保存，显示时才换算成元。"""

from __future__ import annotations


def line_total(unit_price_cents: int, quantity: int, discount_percent: float) -> int:
    """一行的金额（分）：单价 × 数量，再打折；折后不足一分的部分四舍五入（半分进一）。"""
    gross = unit_price_cents * quantity
    return round(gross * (100 - discount_percent) / 100)


def order_total(lines: list[tuple[int, int, float]]) -> int:
    """整单金额（分）：每行先各自取整再相加。"""
    return sum(line_total(price, qty, discount) for price, qty, discount in lines)


def format_yuan(cents: int) -> str:
    """显示成元，保留两位小数。"""
    return f"{cents / 100:.2f}"
