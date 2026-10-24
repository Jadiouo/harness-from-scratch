# v4-subagent 實驗的標準答案（放在 sandbox 之外，模型讀不到）

## 題目（給模型）
> sandbox 裡的 mini_repo 想盤點「折扣」相關的程式。請列出所有跟銷售折扣計算有關的函式：各在哪個檔案、誰呼叫誰；另外指出有沒有「定義了但完全沒人呼叫」的折扣函式。只要回答，不要改任何檔案。

## 標準答案（fixtures/mini_repo；行號以未修改的 fixture 為準）

核心：`shop/pricing/discounts.py`
- `bulk_discount`（:21）、`coupon_discount`（:28）、`member_discount`（:41）：三者**只被 `apply_discounts` 呼叫**（:55、:57、:59）。
- `apply_discounts`（:53）：依序呼叫 bulk -> coupon -> member，回傳 `{bulk, coupon, member, total}`。
  - 經 `shop/pricing/__init__.py:2` re-export，被 `shop/orders.py` 的 `order_summary`（:18，呼叫在 :22）呼叫。
- `loyalty_bonus`（:47）：**定義了但沒人呼叫**（apply_discounts 沒用它；check.py 也沒測）。這是標準答案的關鍵點。

下游（只讀折扣結果，不算折扣；列或不列都可，但不能說它們會計算折扣）
- `shop/pricing/__init__.py:17` `unit_price_after`：讀 `disc["total"]`，**同樣沒人呼叫**（可列為第二個沒人呼叫的相關函式，列出算加分）。
- `shop/reports.py` 的 `sales_report`（:7）、`receipt_text`（:28）：只讀 `summary["discount"]`。
- `shop/orders.py` `order_summary`：用 `disc["total"]` 算折後金額再算運費。

陷阱（不應算進「銷售折扣」）
- `shop/inventory.py:20` `bulk_restock`：供應商進貨的批發折（成本面，不是賣給客人的折扣），只被 `check.py:46-47` 呼叫。若列入必須註明是進貨成本、不屬於訂單折扣。
- `shop/shipping/rates.py` 的免運（`calc_shipping`）不是折扣。

## 評分
- 必有：bulk/coupon/member/apply_discounts 與呼叫關係正確；指出 `loyalty_bonus` 無人呼叫。
- 扣分：把 `bulk_restock` 當成銷售折扣卻不註明；說 `loyalty_bonus` 有被 apply_discounts 呼叫；改了檔案。
