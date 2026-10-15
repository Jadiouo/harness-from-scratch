"""金額格式化與分攤。"""


def format_twd(amount):
    sign = "-" if amount < 0 else ""
    return f"{sign}NT${abs(amount):,}"


def split_evenly(amount, parts):
    """把 amount 平均分成 parts 份，餘數從前面幾份各多分 1 元。"""
    if parts < 1:
        raise ValueError("份數至少為 1")
    base, extra = divmod(amount, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]
