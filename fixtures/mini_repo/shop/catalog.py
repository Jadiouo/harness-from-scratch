"""商品目錄：商品代號 -> 單價（元）與單件重量（公克）。"""

PRICES = {
    "pen": 20,
    "notebook": 50,
    "mug": 150,
    "lamp": 350,
    "bag": 500,
}

WEIGHTS_G = {
    "pen": 15,
    "notebook": 220,
    "mug": 400,
    "lamp": 1200,
    "bag": 900,
}


def get_price(sku):
    if sku not in PRICES:
        raise KeyError(f"未知商品: {sku}")
    return PRICES[sku]


def get_weight(sku):
    return WEIGHTS_G[sku]


def all_skus():
    return sorted(PRICES)


CATEGORIES = {
    "stationery": ["pen", "notebook"],
    "home": ["mug", "lamp"],
    "travel": ["bag"],
}


def category_of(sku):
    for cat, skus in CATEGORIES.items():
        if sku in skus:
            return cat
    raise KeyError(f"商品沒有分類: {sku}")


def skus_in(category):
    return sorted(CATEGORIES.get(category, []))


def cheapest(skus=None):
    """回傳 (價格最低的商品代號, 價格)；同價取代號排序在前。"""
    pool = skus or all_skus()
    return min(((PRICES[s], s) for s in pool))[::-1]
