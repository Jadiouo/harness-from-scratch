"""營業稅：標價為含稅價，這裡只算出其中的稅額。"""

TAX_RATE_PCT = 5


def tax_portion(gross):
    """含稅金額中的稅額（四捨五入到元）。"""
    net = gross * 100 / (100 + TAX_RATE_PCT)
    return round(gross - net)
