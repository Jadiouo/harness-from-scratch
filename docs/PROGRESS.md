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

## 2026-10-15 — v2（權限層）

- v1 已 commit（c69943e）。
- 設計：check_permission → allow/ask/deny；檔案工具路徑出 WORKDIR 即 deny（allow_all 也繞不過）；write_file/edit ask；bash 白名單 allow、其他 ask；拒絕回成 is_error tool_result；**刻意不在 system prompt 提示「被拒絕要換方法」**（觀察實驗）。

### 審查第 1 輪（指揮官自讀＋紅方）
- 驗證：pytest `78 passed`。
- 必修（採納）：白名單指令帶外部路徑（`cat ../.env`）、換行／單一 & 分隔、`env` 前綴、find -fprint 寫外部、python3/pytest 執行任意程式碼（-Sc 繞過 -c 檢查）、`$`/`~`/glob/`cd ..` 展開到外部、symlink 讓 grep/glob 讀外部、NUL 讓 REPL 崩潰、bash 回 a 等於全放行、PERMISSION_MODE 大小寫。
- 修法方向轉變：從「列舉危險」改成「只放行一眼看得出安全的」——限制字元集、拒絕絕對路徑與 ..、白名單縮小並移除所有能執行程式的指令；bash 的 a 只記完全相同的指令。
- 接受為已知限制：字串規則不是安全邊界，核准過的腳本可做任何事，需 OS sandbox；Ctrl-C 中斷時同回應中已執行的工具副作用不回滾。
- 修補後 pytest `121 passed`（2026-10-24 重跑確認）。

### v2 真實測驗第 1 次（logs/v2-run1-all-yes.jsonl）— 未達測驗條件
- 6 次 ask 全部回 y，沒有任何拒絕，所以「拒絕後改走別的路」沒被測到（判定 1、3 FAIL 是因為沒有 n，不是模型問題）。
- 權限層行為正確：read_file/grep 自動放行；每次 edit 與 bash（python3 check.py）都有詢問。
- 觀察：
  - 模型順手改了 check.py 第 65 行的預期字串以配合英文化——合理，但正是「agent 改測試」的典型；權限層讓人有機會在這一步攔下。
  - 模型正確地沒去修無關的 A17 bug，並在回報中說明。
  - 又踩一次 `python` → `python3`（跨 session 沒有記憶；v4 的 CLAUDE.md 載入可解）。
  - 核准疲勞：3 次 bash 指令只差一點點（`python` vs `python3`、多一段 `python3 -c`），「a＝完全相同指令」幫不上忙。Claude Code 用前綴規則（如 `Bash(python3 check.py:*)`）處理。候選改進，未定。

### v2 真實測驗第 2 次（logs/v2-run2-reject.jsonl）— v2 完成標準通過
- 4 項判定全 PASS。第 3 輪模型一次送出 3 個 tool_use：edit reports.py（使用者 n＋理由）、edit check.py（y）、bash 跑 check（n）。
- 被拒絕後沒有重試，改為列出修改對照表並等待確認；且主動指出「同批的 check.py 已經被改了，reports.py 沒改，所以現在不一致，可以幫你改回去」。
- 新發現的 harness 問題：同一則回應裡的多個 tool_use 是互相依賴的計畫，權限層卻逐一獨立詢問 → 拒絕第 1 個、核准第 2 個，造成半套狀態。候選修法：同批中有一個被拒絕，後面的自動以「同批前一個操作被拒絕，未執行」回覆（Claude Code 的拒絕也會中止該回合，待確認細節）。未定，待使用者決定。
- 採納（使用者選 1）：同批連帶略過——同一則回應中第一個被拒絕（n 或 deny）之後的 tool_use 不詢問、不執行，回 is_error「同批前面的 X 被拒絕」，log decision="skipped"；拒絕前已執行的保留真實結果（不回滾）。summarize_log 加第 5 項判定，對修改前錄的 v2-run2-reject.jsonl 正確判 FAIL。pytest `126 passed`。純 harness 邏輯，離線測試即可驗證，未再跑真實 API。

## 2026-10-24 — v3（上下文管理）

- 使用者決定：測驗用 claude-opus-5-5（費用不在意）。
- 設計：
  - 自寫 compaction：上一輪 input+output tokens > COMPACT_AT_TOKENS（預設 20000，刻意設低讓測驗觸發）時，在邊界（最後一則是 user、tool_use 已配對）另呼叫一次 API 產生摘要（tools 照帶、tool_choice none），再把整段歷史原地換成「使用者原文＋摘要」一則 user 訊息。
  - 不採「清掉舊 tool_result」：違反 append-only，會讓 thinking 區塊失效（部分帳號 400）並破壞 prompt cache 前綴。
  - 不用 API 伺服器端 compaction：學習目的要自己寫，只在文件比較。
  - 刻意不在 system prompt 提示「context 可能被壓縮」（觀察摘要本身是否足夠接續）。
  - MAX_TURNS 預設 25 → 60。
  - 測驗 fixture：docstring_repo（約 25 檔、50–70 個函式，補 docstring），check_docstrings.py 驗證全數有 docstring 且函式數不變。

### v3 審查第 1 輪（指揮官自讀＋白方）
- 驗證：pytest `145 passed`（假 client）。
- 採納：
  1. fixture 太小（6.8k 字元），20k 門檻可能不觸發壓縮 → 門檻降 8000，compaction log 記錄門檻供判定使用。
  2. 連續壓縮時 user_texts 把前次壓縮訊息當原文、摘要疊層（已重現）→ 壓縮訊息分塊（標頭／逐段使用者原文／摘要），以固定前綴辨識。
  3. 摘要被 max_tokens 截斷或 refusal 仍被採用 → 檢查 stop_reason；失敗後冷卻 3 輪。
  4. 失憶偵測不準，根因是 log 沒記工具結果（壓縮後查不到）→ 每個 tool_result 寫 log；失憶定義改為「壓縮後重讀已改檔且之後沒再改」。
- 確認無問題：tool_choice none 與 fallbacks 相容（SDK 有型別，推論未實打）；SESSION_ALLOW 壓縮後仍有效；壓縮在邊界做、失敗時歷史不變。

### v3 真實測驗第 1 次（logs/v3-run1-bindfail.jsonl）— 未通過，兩個原因
- **摘要請求 400**：`Invalid signature in thinking block. The block is bound to a different conversation ... The system prompt differs`。官方文件（claude-api skill shared/model-migration.md「Breaking change 3」）：thinking 簽章綁定 top-level system＋tools＋之前所有訊息；tool_choice、max_tokens 等不在綁定範圍。修：摘要請求沿用 SYSTEM_PROMPT 與 TOOLS（刪掉 SUMMARIZER_SYSTEM），只附加摘要指示、tool_choice none。我們的整段替換屬文件推薦的「simple compaction」形狀。冷卻機制如設計運作（失敗後未每輪重試）。pytest `154 passed`。
- **測驗被繞過**：模型讀完 24 檔後，用一次 bash heredoc 在 /tmp 寫 Python 腳本批次插入全部 docstring（使用者回 a 核准）→ 工具呼叫 29 次、ALL OK，但 v3 要測的長任務沒出現。修：任務句加「請用 edit 工具一個函式一個函式修改，不要寫腳本批次處理」。
- 觀察：腳本寫到 /tmp（WORKDIR 外）——權限層有詢問（含 heredoc 判 ask），是人核准的；顯示「a＝這條完全相同的指令」在長指令上等同一次性的 y，核准者要讀內容。

### v3 真實測驗第 2 次（logs/v3-run2-pass.jsonl）— v3 完成標準通過
- 5 項全 PASS：16 輪、91 次工具呼叫（edit 56、read_file 31）、壓縮 4 次，input_tokens 峰值 11,313，每次壓縮後回到約 3.7k–4.1k；ALL OK；失憶偵測 0。總 tokens 148k（估約 0.9 美元）。
- 觀察（未修，候選改進）：
  - **壓縮時機吃掉剛讀的內容**：第 4 輪讀了 6 個 geometry 檔 → 壓縮 → 第 5 輪原樣重讀同 6 檔；第 14 輪跑 check → 壓縮 → 第 15 輪重跑。原因：壓縮發生在 tool_result 剛回來之後，摘要裝不下檔案內容。可能改法：保留最後一輪的 tool_result（需剝掉被保留輪的 thinking，見 keep-tail 規則）、或在讀檔前預估觸發。
  - 模型自動改成「一批讀 6 檔→一批改」的節奏，剛好和壓縮週期對齊。
  - v2 判定區塊在沒有任何 n 時也印出 FAIL，屬顯示問題（只該在有拒絕時判定）。
