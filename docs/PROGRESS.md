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
