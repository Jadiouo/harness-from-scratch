"""訂單：把 {sku: 數量} 轉成價格明細。"""
from utils import validate_items

from .catalog import get_price, get_weight
from .pricing import apply_discounts, subtotal
from .pricing.tax import tax_portion
from .shipping import calc_shipping, parcels_needed


def build_lines(items):
    return [(get_price(sku), qty) for sku, qty in items.items()]


def total_weight(items):
    return sum(get_weight(sku) * qty for sku, qty in items.items())


def order_summary(items, order_id=None, coupon=None, tier=None, region="本島"):
    """回傳訂單明細：小計、各項折扣、運費、總額（含稅價，稅額另列）。"""
    validate_items(items)
    sub = subtotal(build_lines(items))
    disc = apply_discounts(sub, coupon=coupon, tier=tier)
    after = sub - disc["total"]
    fee = calc_shipping(after, region=region)
    total = after + fee
    return {
        "id": order_id,
        "subtotal": sub,
        "discount": disc,
        "shipping": fee,
        "parcels": parcels_needed(total_weight(items)),
        "total": total,
        "tax": tax_portion(total),
    }
