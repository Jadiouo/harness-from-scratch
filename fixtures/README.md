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

## docstring_repo（v3 完成標準：30 輪以上工具呼叫、context 不爆）
24 個小 .py 檔（`geometry/`、`text/`、`stats/`、`convert/`），共 56 個函式，**全都沒有 docstring**，加 `check_docstrings.py`。檢查器用 `ast` 掃描：每個檔能編譯、每個函式有 docstring、函式總數必須等於寫死的 56（防止刪函式取巧）。全對印 `ALL OK`，否則列出缺的 `檔案:函式名` 並以 1 結束。

### 任務提示句（給模型）
> docstring_repo 裡的函式都還沒有 docstring。請逐一讀過每個檔案，幫每個函式補上一行繁體中文 docstring。請用 edit 工具一個函式一個函式修改，不要寫腳本批次處理。完成後執行 python3 check_docstrings.py 確認。

### 複製進 sandbox
```bash
rm -rf sandbox/docstring_repo && cp -r fixtures/docstring_repo sandbox/
```
然後：`.venv/bin/python agent.py "<提示句>"`，再用 `scripts/summarize_log.py`（預設 `--v3-repo sandbox/docstring_repo --v3-fixture fixtures/docstring_repo`）看過程與 v3 判定。

詢問時建議回答：第一次 edit 回 `a`（本 session 允許 edit）；bash 的 `python3 check_docstrings.py` 詢問時回 `a`。

門檻：`COMPACT_AT_TOKENS` 預設 8000（fixture 全部只有約 6.8k 字元，門檻太高會不觸發壓縮）；`summarize_log.py` 優先用 log 裡記錄的 `compact_at`，舊 log 才用 `--compact-at`（預設 8000）。

## v4-記憶實驗（CLAUDE.md 自動載入）
`fixtures/memory/CLAUDE.md` 一句話：這台機器沒有 `python`，一律用 `python3`。

```bash
cp fixtures/memory/CLAUDE.md sandbox/CLAUDE.md
.venv/bin/python agent.py "建立 hello.py 並執行"
.venv/bin/python scripts/summarize_log.py
rm sandbox/CLAUDE.md   # 做完移除，避免影響其他測驗
```
預期：啟動時印 `📒 已載入 sandbox/CLAUDE.md`，第一次 bash 就用 `python3`；`summarize_log.py` 的「v4-記憶判定」三項都過。
對照組：`logs/20261015-225449.jsonl`（v0 那次，沒有記憶）第一次用了 `python`、exit 127 後才改 `python3`。
