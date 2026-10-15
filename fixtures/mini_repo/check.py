"""行為檢查；全對時印 ALL OK，否則逐項列出失敗並以 1 結束。執行：python3 check.py"""
import sys

from shop.catalog import category_of, cheapest
from shop.inventory import Ledger, bulk_restock, needs_restock, reorder_qty
from shop.orders import order_summary
from shop.reports import receipt_text, sales_report, top_skus
from shop.shipping import calc_shipping, parcels_needed
from utils.money import format_twd, split_evenly

failures = []


def expect(label, actual, expected):
    if actual != expected:
        failures.append(f"{label}：預期 {expected}，實際 {actual}")


def order_total(oid, items, expected, **kw):
    s = order_summary(items, order_id=oid, **kw)
    expect(f"訂單 #{oid} 總額錯誤", s["total"], expected)
    return s


# 訂單總額
order_total("A01", {"pen": 2}, 100)
order_total("A05", {"bag": 1}, 500)
order_total("A12", {"bag": 2, "notebook": 1}, 945)
order_total("A17", {"bag": 2}, 900)
order_total("A20", {"lamp": 2}, 650, coupon="WELCOME50")
order_total("A21", {"bag": 1, "lamp": 1}, 765, coupon="SAVE10")
order_total("A22", {"bag": 1, "notebook": 6}, 720, coupon="SAVE10")
order_total("A23", {"lamp": 2}, 665, tier="gold")
order_total("A24", {"pen": 1}, 140, region="離島")

# 運費與包裹
expect("本島運費(499)", calc_shipping(499), 60)
expect("本島運費(500)", calc_shipping(500), 0)
expect("包裹數(5000g)", parcels_needed(5000), 1)
expect("包裹數(5001g)", parcels_needed(5001), 2)

# 庫存
expect("補貨判斷(10)", needs_restock(10), True)
expect("補貨判斷(11)", needs_restock(11), False)
expect("補貨量", reorder_qty(4, incoming=3), 53)
expect("進貨成本(99 件)", bulk_restock(99, 10), 990)
expect("進貨成本(100 件)", bulk_restock(100, 10), 920)

# 報表（不含 A17）
rows = [order_summary({"pen": 2}), order_summary({"bag": 1}), order_summary({"bag": 2, "notebook": 1})]
rep = sales_report(rows)
expect("報表訂單數", rep["orders"], 3)
expect("報表營收", rep["revenue"], 100 + 500 + 945)
expect("報表免運單數", rep["free_shipping"], 2)
expect("熱銷排行", top_skus([{"bag": 2, "pen": 2}, {"bag": 1, "notebook": 1}]),
       [("bag", 3), ("pen", 2), ("notebook", 1)])

# 帳本與商品
led = Ledger({"pen": 12, "mug": 3})
led.ship({"pen": 2, "mug": 1})
expect("出貨後庫存", led.on_hand, {"pen": 10, "mug": 2})
expect("低庫存清單", led.low_stock(), ["mug", "pen"])
expect("商品分類", category_of("lamp"), "home")
expect("最便宜商品", cheapest(), ("pen", 20))
expect("收據內容", receipt_text(order_summary({"pen": 2}, order_id="R1")).splitlines()[-1], "總額  NT$100（含稅 NT$5）")

# 工具函式
expect("金額格式", format_twd(1234567), "NT$1,234,567")
expect("均分金額", split_evenly(100, 3), [34, 33, 33])

# 非法輸入
try:
    order_summary({"pen": 0})
    failures.append("數量為 0 應該被拒絕")
except ValueError:
    pass

if failures:
    print("\n".join(f"FAIL {f}" for f in failures), file=sys.stderr)
    sys.exit(1)
print("ALL OK")
