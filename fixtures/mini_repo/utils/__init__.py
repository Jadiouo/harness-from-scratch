"""共用小工具。"""
from .money import format_twd, split_evenly

MAX_QTY_PER_SKU = 999


def validate_items(items):
    """檢查 {sku: 數量}：不可為空、數量要是 1..999 的整數。"""
    if not items:
        raise ValueError("訂單不可為空")
    for sku, qty in items.items():
        if not isinstance(qty, int) or isinstance(qty, bool):
            raise ValueError(f"{sku} 的數量必須是整數")
        if qty < 1 or qty > MAX_QTY_PER_SKU:
            raise ValueError(f"{sku} 的數量超出範圍")


__all__ = ["format_twd", "split_evenly", "validate_items", "MAX_QTY_PER_SKU"]
