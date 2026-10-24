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

## docstring_repo（v3 完成標準：30 輪以上工具呼叫；在人為壓低的門檻 8,000 tokens 下驗證壓縮機制，不是證明長任務不會爆 context）
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
對照組：同一版程式、`sandbox/` 裡沒有 `CLAUDE.md` 的那次（`logs/v4-mem-control.jsonl`）：第 2 輪用了 `python`、exit 127，第 3 輪才改 `python3`。

## v4-TODO 實驗（TODO 工具：模型會不會自發使用）
沿用 v3 的 docstring_repo 和任務句，**任務句不提 TODO、system prompt 也沒有 TODO 說明**，只有 `todo_write` 的工具描述；觀察模型會不會自己先列清單、邊做邊更新。

```bash
rm -rf sandbox/docstring_repo && cp -r fixtures/docstring_repo sandbox/
.venv/bin/python agent.py "docstring_repo 裡的函式都還沒有 docstring。請逐一讀過每個檔案，幫每個函式補上一行繁體中文 docstring。請用 edit 工具一個函式一個函式修改，不要寫腳本批次處理。完成後執行 python3 check_docstrings.py 確認。"
.venv/bin/python scripts/summarize_log.py
```
詢問時的回答方式同 v3（edit 回 `a`、`python3 check_docstrings.py` 回 `a`）。`todo_write` 不會被詢問。

看 `summarize_log.py` 的「v4-TODO 判定」：先規劃再動手、更新 >= 3 次、最後全 completed、每次壓縮都帶 todos；另有壓縮後下一輪的工具呼叫與失憶偵測（對照 v3 同任務的 `reread` 次數）。
預期（**實測：模型沒用 TODO 工具**，只靠工具描述 0 次；補 system prompt 一句後才用，見 `docs/PROGRESS.md`「v4-2 真實測驗」）：若模型沒用 TODO 工具，會印「⚠ 長任務但沒用 TODO 工具」——這本身就是實驗結果，代表只靠工具描述不夠，下一步才考慮在 system prompt 加說明。

## v4-subagent 實驗（task 工具：主對話 context 有沒有保持乾淨）
唯讀題目：盤點 mini_repo 裡跟折扣有關的函式與呼叫關係。標準答案在 `fixtures/subagent_answer.md`（sandbox 之外）。要看的是**同一題**分成兩組跑，比較主對話的 input_tokens 峰值與答案。

```bash
rm -rf sandbox/mini_repo && cp -r fixtures/mini_repo sandbox/
# 實驗組
.venv/bin/python agent.py "sandbox 裡的 mini_repo 想盤點「折扣」相關的程式。請列出所有跟銷售折扣計算有關的函式：各在哪個檔案、誰呼叫誰；另外指出有沒有「定義了但完全沒人呼叫」的折扣函式。只要回答，不要改任何檔案。請用 task 工具派 subagent 去搜尋，你自己不要直接讀檔或搜尋。"
.venv/bin/python scripts/summarize_log.py
mv "$(ls -t logs/*.jsonl | head -1)" logs/v4-sub-run-task.jsonl

# 對照組（同一個 mini_repo，沒改過）
.venv/bin/python agent.py "sandbox 裡的 mini_repo 想盤點「折扣」相關的程式。請列出所有跟銷售折扣計算有關的函式：各在哪個檔案、誰呼叫誰；另外指出有沒有「定義了但完全沒人呼叫」的折扣函式。只要回答，不要改任何檔案。不要使用 task 工具。"
.venv/bin/python scripts/summarize_log.py
```
對照組沒有 sub 行，所以不會印 v4-subagent 判定；mini_repo 沒被修好，v1 判定會是 FAIL，這題不看它。

看法：
1. `summarize_log.py` 的「v4-subagent 判定」只在實驗組出現：呼叫 task、subagent 有搜尋、task 回傳字元數 < subagent 收到工具結果的 20%；下面「觀察」列主對話峰值 input_tokens、subagent 總 tokens、主對話在 task 之後有沒有自己又搜尋。
2. 兩組都看每輪摘要的 `in=`：對照組的 input 會隨著讀檔一路長高；實驗組主對話只有少數幾輪、input 應該停在低檔。
3. 兩組的最終答案都對照 `fixtures/subagent_answer.md`（尤其是 `loyalty_bonus` 沒人呼叫、`bulk_restock` 不是銷售折扣）。

預期（實測大致相符，見 `docs/PROGRESS.md`「v4-3 真實測驗」）：
- 實驗組：主對話約 2-3 輪（派 task、收結論、回報），input_tokens 峰值遠低於對照組；subagent 約 5-10 輪 grep/read_file；task 回傳約數百字元，比例遠低於 20%，判定 1-3 PASS；主對話之後不重做搜尋。
- 對照組：主對話自己 grep/read_file，input_tokens 隨輪數增加，峰值高於實驗組。
- 兩組答案都應包含 bulk/coupon/member/apply_discounts 與 `loyalty_bonus` 無人呼叫；subagent 若漏看 pricing/__init__.py 的 re-export，可能說不出 orders.py 怎麼呼叫，這是 subagent 只回結論的代價（細節遺失）。
- 這個 repo 只有約 400 行，總 token 不一定省（subagent 的 token 照付）；要看的是主對話峰值。

## v4-hooks 實驗（post_tool_use hook：規則違反時回報給模型）
任務句不提「不能用 print」，規則只存在於 hook（`fixtures/hooks/no_print_hook.py`）；觀察模型被回報後會不會自己改用 logging。hooks 設定檔放在 `.agent/hooks.json`（WORKDIR 之外、已在 `.gitignore`）。

```bash
rm -rf sandbox/mini_repo && cp -r fixtures/mini_repo sandbox/
mkdir -p .agent && cp fixtures/hooks/hooks.json .agent/hooks.json
.venv/bin/python agent.py "在 mini_repo 的 order_summary 裡加一行除錯輸出，印出每張訂單的小計。改完在 mini_repo 目錄裡跑 python3 check.py 確認沒壞。"
.venv/bin/python scripts/summarize_log.py
rm .agent/hooks.json   # 做完移除，避免影響其他測驗
```
WORKDIR 是 `sandbox/`，所以任務句寫 `mini_repo/`；check.py 在 `sandbox/mini_repo/check.py`，模型的 bash 要先 `cd mini_repo`（bash 詢問時照 v2 的方式回答即可）。edit 會被問，回 `a`。

**check.py 的基準**：fixture 的 mini_repo 原本就有已知 bug，check.py 本來就會 FAIL；這個實驗看的是「沒有新增失敗」（失敗清單與 `cd fixtures/mini_repo && python3 check.py` 的原始輸出相同），不是 ALL OK。所以 `summarize_log.py` 在這份 log 不做 v1 判定。

預期（實測相符，見 `docs/PROGRESS.md`「v4-4 真實測驗」）：模型先用 `print` → 終端出現 `🪝 post hook 回報` → 下一步 edit 改成 `logging` → hook 通過（exit 0）；check.py 的失敗清單與原本相同。看 `summarize_log.py` 的「v4-hooks 判定」。
