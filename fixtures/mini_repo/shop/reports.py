"""把多張訂單明細彙整成報表。"""
from collections import Counter

from utils import format_twd


def sales_report(summaries):
    """summaries: order_summary 的結果列表。"""
    n = len(summaries)
    revenue = sum(s["total"] for s in summaries)
    return {
        "orders": n,
        "revenue": revenue,
        "discount_total": sum(s["discount"]["total"] for s in summaries),
        "avg_order": revenue // n if n else 0,
        "free_shipping": sum(1 for s in summaries if s["shipping"] == 0),
    }


def top_skus(all_items, n=3):
    """all_items: [{sku: 數量}, ...]；回傳銷量前 n 名 [(sku, 數量)]，同量依代號排序。"""
    c = Counter()
    for items in all_items:
        c.update(items)
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def receipt_text(summary):
    """純文字收據，一行一項。"""
    rows = [f"訂單 {summary['id'] or '(未編號)'}", f"小計  {format_twd(summary['subtotal'])}"]
    for name, label in (("bulk", "大量折扣"), ("coupon", "優惠券"), ("member", "會員折扣")):
        if summary["discount"][name]:
            rows.append(f"{label}  -{format_twd(summary['discount'][name])}")
    rows.append(f"運費  {format_twd(summary['shipping'])}")
    rows.append(f"總額  {format_twd(summary['total'])}（含稅 {format_twd(summary['tax'])}）")
    return "\n".join(rows)
