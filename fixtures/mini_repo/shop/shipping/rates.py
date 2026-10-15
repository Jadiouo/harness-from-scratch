"""運費：依地區收費，折後金額夠高就免運；另外算包裹數。"""

FREE_SHIPPING_THRESHOLD = 500   # 折後滿 500 元免運
REGION_FEES = {"本島": 60, "離島": 120}
MAX_PARCEL_G = 5000             # 每個包裹最重 5 公斤


def calc_shipping(amount, region="本島"):
    if region not in REGION_FEES:
        raise ValueError(f"不支援的地區: {region}")
    if amount >= FREE_SHIPPING_THRESHOLD:
        return 0
    return REGION_FEES[region]


def parcels_needed(weight_g):
    if weight_g <= 0:
        return 0
    return -(-weight_g // MAX_PARCEL_G)   # 無條件進位


def estimate_days(region="本島", express=False):
    """預估送達天數。"""
    base = 2 if region == "本島" else 4
    return max(base - 1, 1) if express else base
