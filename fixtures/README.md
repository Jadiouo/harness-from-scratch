# fixtures：v1 完成標準測驗用 repo

## mini_repo
一個迷你訂單計算 package（`shop/`、`utils/`）加 `check.py`。全對時印 `ALL OK`，否則列出失敗的項目並以 1 結束。

## 任務提示句（給模型）
> sandbox 裡的 mini_repo 執行 python3 check.py 會失敗，請找出原因並修好。

## 複製進 sandbox
```bash
rm -rf sandbox/mini_repo && cp -r fixtures/mini_repo sandbox/
```
然後：`.venv/bin/python agent.py "<提示句>"`，再用 `scripts/summarize_log.py` 看過程與完成標準判定。
