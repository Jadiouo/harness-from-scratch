"""價格規則：小計在這裡，折扣在 discounts，稅額在 tax。金額一律用整數元。"""
from .discounts import apply_discounts


def line_total(unit_price, qty):
    """單一品項小計。"""
    if qty < 0:
        raise ValueError("數量不可為負")
    return unit_price * qty


def subtotal(lines):
    """lines: [(單價, 數量), ...]"""
    return sum(line_total(price, qty) for price, qty in lines)


def unit_price_after(sub, disc, qty_total):
    """折後平均單價（無條件捨去），報表與對帳用。"""
    return (sub - disc["total"]) // qty_total if qty_total else 0


__all__ = ["apply_discounts", "line_total", "subtotal", "unit_price_after"]
