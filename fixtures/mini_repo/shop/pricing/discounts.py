"""折扣規則：大量折扣、優惠券、會員折扣。依序套用，每一步都以前一步折後金額計算。"""

# 大量採購折扣：小計滿 1000 元打 9 折
BULK_DISCOUNT_THRESHOLD = 1000
BULK_DISCOUNT_PCT = 10

# 優惠券：(類型, 數值)；SAVE10 另有最低消費
COUPONS = {
    "WELCOME50": ("flat", 50),
    "SAVE10": ("pct", 10),
}
COUPON_MIN_SPEND = {"SAVE10": 800}

# 會員等級折扣百分比
TIER_PCT = {"silver": 3, "gold": 5}

# 回頭客：累積訂單數達到這個數目才享有額外 1% 折扣
LOYALTY_MIN_ORDERS = 5


def bulk_discount(amount):
    """回傳要折掉的金額。"""
    if amount > BULK_DISCOUNT_THRESHOLD:
        return amount * BULK_DISCOUNT_PCT // 100
    return 0


def coupon_discount(amount, code):
    """優惠券折抵金額；未達最低消費回傳 0。折抵不會超過金額本身。"""
    if code is None:
        return 0
    if code not in COUPONS:
        raise ValueError(f"無效的優惠券: {code}")
    if amount < COUPON_MIN_SPEND.get(code, 0):
        return 0
    kind, value = COUPONS[code]
    off = value if kind == "flat" else amount * value // 100
    return min(off, amount)


def member_discount(amount, tier):
    if tier is None:
        return 0
    return amount * TIER_PCT[tier] // 100


def loyalty_bonus(amount, past_orders):
    if past_orders >= LOYALTY_MIN_ORDERS:
        return amount // 100
    return 0


def apply_discounts(sub, coupon=None, tier=None):
    """依序套用大量折扣 -> 優惠券 -> 會員折扣，回傳各項與合計。"""
    bulk = bulk_discount(sub)
    after = sub - bulk
    cp = coupon_discount(after, coupon)
    after -= cp
    mem = member_discount(after, tier)
    return {"bulk": bulk, "coupon": cp, "member": mem, "total": bulk + cp + mem}
