# 進度

## 2026-10-15 — v0

### 已定案的決定
- 語言：Python（handoff 預設）；TypeScript 仍是開放問題，v0 不處理。
- 手寫 agent loop，不用 SDK tool runner——學習目的是看見迴圈。
- 模型 `claude-opus-5-5`、effort `medium`、非 streaming、`max_tokens=16000`，可用環境變數覆寫。
- 開啟 server-side refusal fallback（`fallbacks="default"`），`USE_FALLBACKS=0` 可關。
- 歷史一律 append-only：assistant 回應整包存回，不改寫舊訊息（保留 thinking 區塊、符合 preserved thinking 規則）。
- bash cwd 固定在 `sandbox/`（handoff 安全要求：v2 前只在丟棄式資料夾跑）。
- 達 MAX_TURNS 時，下一句使用者輸入併入最後一則 user 訊息，確保 tool_use/tool_result 成對。

### 審查第 1 輪（白方，段落中途）
- 驗證：pytest `7 passed`（假 client，未打真實 API）。
- 必改（採納）：A 非 tool_use 終止分支（max_tokens/refusal/其他）留下孤兒 tool_use；B 空 content assistant；C API 例外與 Ctrl-C 讓程式崩潰／留孤兒。修法：`close_dangling()` 放在 run_turn 的 finally，加 APIError 與 KeyboardInterrupt 處理。
- 文件：pause_turn 註解改成如實說明（v0 走不到）；說明文件流程圖改正。
- 不採納：D（/reset 前後共用一個 log）——一個 session 一個 log 本來就合理，回放時看得到 reset 點；E（tool input 非 dict）——API 保證 input 是物件，風險極低。
- 待真實 API 驗證（無法離線確認）：fallback 換模型時，回放 thinking 區塊是否被接受；SDK 回應物件直接放回 messages 是否被接受。→ 煙霧測試時 USE_FALLBACKS=0/1 各跑一次。

### 狀態
- [x] repo、venv、anthropic 1.14.0
- [x] v0 實作＋離線測試：審查第 1 輪必改項已修，pytest `13 passed`（指揮官重跑確認）
- [x] 真實 API 跑「建立 hello.py 並執行」（log：logs/20261015-225449.jsonl）——**v0 完成標準通過**
  - 3 輪，claude-opus-5-5，USE_FALLBACKS=1。第 1 輪一則回應含 write_file＋bash 兩個 tool_use；bash `python` 回 exit 127，模型讀 stderr 後第 2 輪改用 `python3` 成功；第 3 輪 end_turn 正確回報。
  - sandbox/hello.py 實際存在並可執行，輸出 `Hello, World!`。
  - 已確認：SDK 回應物件原樣放回 messages 可被 API 接受；歷史配對合法。
  - 未驗證：fallback 實際觸發時的行為（本次沒發生 refusal）；USE_FALLBACKS=0 那次沒跑，影響小。

## 2026-10-15 — v1（edit、grep、glob）

- v0 已 commit（f74645b）。
- 設計：edit 要求 old_string 唯一（照 Claude Code Edit）；grep/glob 用 Python 實作不依賴 rg；read_file 加行號＋offset/limit。

### 審查第 1 輪（白方）
- 驗證：pytest `27 passed`；fixture 確實失敗。
- 採納：edit 把 CRLF 轉成 LF；read_file 2000 行預設與 1 萬字元通用截斷不一致（中段被砍、續讀提示消失）；grep 路徑不存在時誤回「沒有結果」；grep 的 glob 只比對檔名；read_file 邊界輸入；非 UTF-8 錯誤訊息。
- 採納（測驗設計）：fixture 太小、錯誤訊息直接洩漏答案、答案寫在模型用 bash 讀得到的 README、summarize_log 不會自動判定通過 → fixture 擴大＋干擾項、答案只放在測試、加自動判定與「偷看答案」偵測。
- 不採納：glob/_path 可逃出 WORKDIR——屬 v2 權限層範圍。

### 第一次真實測驗沒跑成（非模型問題）
- 原因：新開的終端機沒有 API key，第一個請求就丟 TypeError；而 summarize_log 默默拿 v0 的舊 log 做判定。
- 修：啟動時檢查 key、認證錯誤印一行不噴 traceback、API 失敗也寫 log、summarize_log 遇到比 repo 舊或不含 mini_repo 的 log 會警告並 exit 2。
- 使用者要求：key 存在專案資料夾 → 專案根目錄 `.env`（已 gitignore，附 `.env.example`），main 啟動時載入，終端機 export 的值優先。
- 同時：bash 工具的子程序移除 ANTHROPIC_* 環境變數，避免模型把 key 印進對話與 log。
- 已知未擋：模型仍可用 bash `cat ../.env` 讀到 key（路徑限制屬 v2）。
- pytest `46 passed`。

### v1 真實測驗 A（logs/v1-A-baseline.jsonl）
- 結果：1/2/3/5/6 PASS，4 FAIL。bug 正確修好（discounts.py `>`→`>=`，1 行），但 6 輪中 4 輪用 bash cat/find，第 4 輪 `for ... cat -n` 把 8 個檔全讀；grep/glob/read_file 0 次。input tokens 4818→8585。
- 判斷：v1 完成標準「先搜再讀再精準改」未達成，屬工具重疊時模型偏好 bash，非程式 bug。
- 實驗 B：只改 bash 工具描述（加「不要用 bash 讀檔/搜尋，改用專用工具」，仿 Claude Code），任務句不變。summarize 的繞過偵測補上 cat/head/tail/ls -R。
- 另觀察：任務句「sandbox 裡的 mini_repo」讓模型第 1 輪 cd sandbox/mini_repo 失敗（WORKDIR 已是 sandbox）；為保持單一變數，B 組暫不改。

### v1 真實測驗 B（logs/v1-B-bash-desc.jsonl）— v1 完成標準通過
- 6 項全 PASS。工具：glob 3、read_file 4、edit 1、bash 4（全用於執行 check.py／pwd，0 個繞過）。
- 對照 A：只改 bash 描述一句，模型就從 bash cat/find 改用 glob＋read_file。
- 預期落空：總 tokens 沒減少（A 31,830 → B 36,445）。原因：任務句路徑混淆在 B 浪費 2 輪（glob sandbox/mini_repo/** 找不到）；且仍是讀整檔（4 個檔），沒用 grep 定位。「搜尋」是靠 glob 列檔名猜到 discounts.py，不是內容搜尋。
- 修：summarize_log 的 latest_log 改依 mtime（原本依檔名，把 v1-A-baseline 當最新）。
