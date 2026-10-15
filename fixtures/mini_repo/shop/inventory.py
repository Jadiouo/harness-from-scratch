"""庫存補貨規則與簡單的進出貨帳本。"""

REORDER_POINT = 10            # 庫存降到這個數量就要補貨
TARGET_LEVEL = 60             # 補貨補到這個水位
BULK_RESTOCK_THRESHOLD = 100  # 單次進貨滿 100 件，供應商給批發價
BULK_RESTOCK_DISCOUNT_PCT = 8


def needs_restock(on_hand):
    return on_hand <= REORDER_POINT


def reorder_qty(on_hand, incoming=0):
    """要再下訂的數量；已在途的進貨量要先扣掉。"""
    if not needs_restock(on_hand + incoming):
        return 0
    return max(TARGET_LEVEL - on_hand - incoming, 0)


def bulk_restock(qty, unit_cost):
    """回傳這次進貨的總成本；數量達批發線就打折。"""
    cost = qty * unit_cost
    if qty >= BULK_RESTOCK_THRESHOLD:
        cost -= cost * BULK_RESTOCK_DISCOUNT_PCT // 100
    return cost


class Ledger:
    """每個商品的現有庫存；出貨不可讓庫存變負。"""

    def __init__(self, initial=None):
        self.on_hand = dict(initial or {})

    def receive(self, sku, qty):
        self.on_hand[sku] = self.on_hand.get(sku, 0) + qty

    def ship(self, items):
        # 先整批檢查再扣，避免扣到一半才發現缺貨
        short = [s for s, q in items.items() if self.on_hand.get(s, 0) < q]
        if short:
            raise ValueError(f"庫存不足: {', '.join(sorted(short))}")
        for sku, qty in items.items():
            self.on_hand[sku] -= qty

    def low_stock(self):
        return sorted(s for s, q in self.on_hand.items() if needs_restock(q))
