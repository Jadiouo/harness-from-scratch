# 進度

## 用語與證據分級（第 1 輪閉門會議後統一，2026-10-24）
- **n=1**：本檔所有真實 API 的對照實驗（v1 A/B、v4 記憶、v4 TODO 無／有提示、v4 subagent、hooks）都只各跑 1 次，屬**示範**，不是統計結論；跨次變異沒有量。行為觀察（工具選擇、是否自發用 TODO）一律只能讀成「在這個任務、這一次」。
- **證據三級**：(1) 決定性機制事實（API 400、離線測試證明的不變量）；(2) 單次實測的量測值（token、輪數）；(3) 單次的行為觀察（模型選了哪個工具、是否改走別的路）。
- **峰值**：統一定義為「單次請求的 `input_tokens` 最大值」，並一律標註範圍：「主對話各輪」不含摘要請求；「摘要請求本身」另列。`summarize_log.py` 的 v3 第 5 項與 v4-subagent 的「主對話 input_tokens 峰值」輸出則是兩者合併取最大（含摘要請求），與本檔標註「主對話各輪」的數字口徑不同，比較時先看標註。
- 以下各節保留原始結果，只在標題或結論處補上分級與口徑。

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
- [x] 真實 API 跑「建立 hello.py 並執行」（log：evidence/logs/20261015-225449.jsonl）——**v0 完成標準通過**
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

### v1 真實測驗 A（evidence/logs/v1-A-baseline.jsonl）
- 結果：1/2/3/5/6 PASS，4 FAIL。bug 正確修好（discounts.py `>`→`>=`，1 行），但 6 輪中 4 輪用 bash cat/find，第 4 輪 `for ... cat -n` 把 8 個檔全讀；grep/glob/read_file 0 次。input tokens 4818→8585。
- 判斷：v1 完成標準「先搜再讀再精準改」未達成，屬工具重疊時模型偏好 bash，非程式 bug。
- 實驗 B：只改 bash 工具描述（加「不要用 bash 讀檔/搜尋，改用專用工具」，仿 Claude Code），任務句不變。summarize 的繞過偵測補上 cat/head/tail/ls -R。
- 另觀察：任務句「sandbox 裡的 mini_repo」讓模型第 1 輪 cd sandbox/mini_repo 失敗（WORKDIR 已是 sandbox）；為保持單一變數，B 組暫不改。

### v1 真實測驗 B（evidence/logs/v1-B-bash-desc.jsonl）— v1 完成標準通過
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

### v2 真實測驗第 1 次（evidence/logs/v2-run1-all-yes.jsonl）— 未達測驗條件
- 5 次 ask（edit 2、bash 3）全部回 y，沒有任何拒絕，所以「拒絕後改走別的路」沒被測到（判定 1、3 FAIL 是因為沒有 n，不是模型問題）。
- 權限層行為正確：read_file/grep 自動放行；每次 edit 與 bash（python3 check.py）都有詢問。
- 觀察：
  - 模型順手改了 check.py 第 65 行的預期字串以配合英文化——合理，但正是「agent 改測試」的典型；權限層讓人有機會在這一步攔下。
  - 模型正確地沒去修無關的 A17 bug，並在回報中說明。
  - 又踩一次 `python` → `python3`（跨 session 沒有記憶；v4 的 CLAUDE.md 載入可解）。
  - 核准疲勞：3 次 bash 指令只差一點點（`python` vs `python3`、多一段 `python3 -c`），「a＝完全相同指令」幫不上忙。Claude Code 用前綴規則（如 `Bash(python3 check.py:*)`）處理。候選改進，未定。

### v2 真實測驗第 2 次（evidence/logs/v2-run2-reject.jsonl）— v2 完成標準通過（但「改走別的路」受拒絕理由混淆，見下；n=1）
- 4 項判定全 PASS。第 3 輪模型一次送出 3 個 tool_use：edit reports.py（使用者 n＋理由）、edit check.py（y）、bash 跑 check（n）。
- 被拒絕後沒有重試，改為列出修改對照表並等待確認（這與當時填的拒絕理由「先把你打算怎麼改列給我看」一致，所以證明的是「模型會讀拒絕理由並照辦」；「理由留空時能否自己換方法」沒有測到）；且主動指出「同批的 check.py 已經被改了，reports.py 沒改，所以現在不一致，可以幫你改回去」。
- 證據分級（降級）：此次拒絕理由本身就引導模型照做（混淆），所以「拒絕後改走別的路」只能讀成「模型會讀拒絕理由並照辦、不盲目重試」；光靠「被拒絕」這個事實是否會自己換方法，沒有隔離、沒有測到（要測需回 `n` 且理由留空並重跑）。屬單次行為觀察。
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

### v3 真實測驗第 1 次（evidence/logs/v3-run1-bindfail.jsonl）— 未通過，兩個原因
- **摘要請求 400**：`Invalid signature in thinking block. The block is bound to a different conversation ... The system prompt differs`。官方文件（出處：Anthropic claude-api 文件的 model migration 指南「Breaking change 3」，preserved thinking；非公開網址，以文字出處為準）：thinking 簽章綁定 top-level system＋tools＋之前所有訊息；tool_choice、max_tokens 等不在綁定範圍。修：摘要請求沿用 SYSTEM_PROMPT 與 TOOLS（刪掉 SUMMARIZER_SYSTEM），只附加摘要指示、tool_choice none。我們的整段替換屬文件推薦的「simple compaction」形狀。冷卻機制如設計運作（失敗後未每輪重試）。pytest `154 passed`。
- **測驗被繞過**：模型讀完 24 檔後，用一次 bash heredoc 在 /tmp 寫 Python 腳本批次插入全部 docstring（使用者回 a 核准）→ 工具呼叫 29 次、ALL OK，但 v3 要測的長任務沒出現。修：任務句加「請用 edit 工具一個函式一個函式修改，不要寫腳本批次處理」。
- 觀察：腳本寫到 /tmp（WORKDIR 外）——權限層有詢問（含 heredoc 判 ask），是人核准的；顯示「a＝這條完全相同的指令」在長指令上等同一次性的 y，核准者要讀內容。

### v3 真實測驗第 2 次（evidence/logs/v3-run2-pass.jsonl）— v3 完成標準通過（人為低門檻下的機制測試，n=1）
- 證據分級（降級）：`COMPACT_AT_TOKENS=8000` 是人為壓低的門檻（真實模型上限高得多），任務句也被指定「用 edit 逐函式修改、不要寫腳本」。所以這次驗證的是「壓縮在密集觸發（4 次）下能運作並接得上任務」，**不是**「長任務不會爆 context」的上限證明，也沒測過沒有壓縮時會不會爆；「context 有界」只是相對於這個人為門檻。
- 5 項全 PASS：16 輪、91 次工具呼叫（edit 56、read_file 31）、壓縮 4 次，主對話各輪 input 最高 9,858（4 次摘要請求本身的 input 最高 11,313），每次壓縮後回到約 3.7k–4.1k；ALL OK；失憶偵測 0。總 tokens 148k（主對話 102k＋摘要請求 46k）。
- 觀察（未修，候選改進）：
  - **壓縮時機吃掉剛讀的內容**：第 4 輪讀了 6 個 geometry 檔 → 壓縮 → 第 5 輪原樣重讀同 6 檔；第 14 輪跑 check → 壓縮 → 第 15 輪重跑。原因：壓縮發生在 tool_result 剛回來之後，摘要裝不下檔案內容。可能改法：保留最後一輪的 tool_result（需剝掉被保留輪的 thinking，見 keep-tail 規則）、或在讀檔前預估觸發。
  - 模型自動改成「一批讀 6 檔→一批改」的節奏，剛好和壓縮週期對齊。
  - v2 判定區塊在沒有任何 n 時也印出 FAIL，屬顯示問題（只該在有拒絕時判定）。

## v4-1 專案記憶（CLAUDE.md 自動載入）
- 設計（定案）：只讀 `WORKDIR/CLAUDE.md`（不往上層找）；session 開始時接在 system 後面、之後凍結（thinking 簽章綁定 system，中途重組會 400，見 v3 第 1 次）；放 system 而非第一則 user 訊息，壓縮時自動保留。代價：中途改 CLAUDE.md 要下次啟動才生效；/reset 不重讀。上限 4000 字。REPL `/memory`。
- 順手修：summarize_log 的 v2 判定只在有 n 或 deny 時才印。

### v4-1 審查第 1 輪（sonnet，乾淨上下文）
- 驗證：pytest `169 passed`；指揮官確認 SESSION_SYSTEM 只在 main 設定一次。
- 必改（皆採納）：
  1. load_memory 跟著 symlink 讀 WORKDIR 外的檔（實測會把外部內容送上 API）→ 用 `_inside()` 拒載。
  2. judge_v4 沒涵蓋**失敗**的摘要請求（v3 的 400 正是這種）→ `compaction_failed` 也記 system 並納入比對。
- 建議（皆採納）：空白檔視為沒有；目錄測試；刪空洞斷言改經 main 的 /reset 測試；docs「模型較重視 system」標假設。
- 修正後驗證：pytest `175 passed`；指揮官實測 symlink 指向 `.scratch/` 外部檔 → 印警告、回 None。無必改項，進真實測驗。
- 真實測驗見下節。

### v4-1 真實測驗（同一版程式，只差有沒有 sandbox/CLAUDE.md）— 通過
- 有記憶（evidence/logs/v4-mem-with.jsonl）：第 1 輪就 `python3 hello.py`，2 輪完成；v4 判定全 PASS（system 2 個請求皆相同）。
- 對照組無記憶（evidence/logs/v4-mem-control.jsonl）：第 2 輪 `python hello.py` 失敗，第 3 輪才 `python3`，4 輪完成。
- 結論（降級）：在這個任務、這一次，有 CLAUDE.md 省掉一次「重新發現環境限制」的試錯。樣本各 1 次（n=1），屬示範而非統計。

## v4-2 TODO 工具（todo_write）
- 設計（定案）：每次交整份清單、整份取代；狀態存 harness（TODOS）；system prompt 只列工具名、不寫 TODO 用法（觀察只靠工具描述會不會自發使用）；權限直接 allow；壓縮時在摘要後附 `【目前 TODO】` 區塊（清單是 harness 持有的事實，不靠摘要記）；/todo、/reset 清空。
- 指揮官自修：SYSTEM_PROMPT「六個工具」→ 列出七個（與實際一致）；todo_write 清單在終端重複印兩次 → 只印一次。

### v4-2 審查第 1 輪（sonnet，乾淨上下文）
- 驗證：pytest `195 passed`。
- 必改（皆採納）：judge_todo 把失敗/被略過的 todo_write 也採計；壞輸入會讓 judge 崩；無壓縮時第 4 項空洞 PASS；工具描述說「不靜默修正」但實作丟多餘欄位、content 換行會破壞區塊格式、無長度上限。
- 建議採納：最後清空清單不算 FAIL（看最後一份非空清單）；文件註明 bash 寫檔不算「動手」。
- 不採納：使用者輸入剛好以 `【目前 TODO】` 開頭會被吃掉（機率極低）；REPL 沒 /reset 時 TODOS 延續到下一任務（刻意：清單屬於 session，下一任務會整份取代；已寫進文件）。
- 修正後驗證：pytest `206 passed`；指揮官實測 todo_write 正常、換行與多餘欄位皆 is_error、權限 allow。無必改項，進真實測驗。

### v4-2 真實測驗第 1 次（evidence/logs/v4-todo-run1-nohint.jsonl）— 模型沒用 TODO
- 條件：只有工具描述引導，system prompt 只列工具名。
- 結果：90 次工具呼叫（read_file 31、edit 56）、壓縮 4 次，todo_write **0 次**；v3 判定全 PASS（ALL OK），失憶偵測 0。
- 解讀（推論）：任務本身有天然分段（按資料夾一批讀、一批改），模型不需要外部清單；只靠工具描述不足以讓它自發使用。
- 觀察：壓縮後有 1 次原樣重讀（第 4→5 輪 geometry 6 檔），另外 3 次壓縮後直接 edit 沒重讀（摘要帶到足夠內容）。
- 下一步：system prompt 加一句「三步以上先 todo_write 列清單、每完成一項更新、壓縮後照清單接著做」，同任務重跑當實驗組（對照組即本次）。pytest `206 passed`。

### v4-2 真實測驗第 2 次（evidence/logs/v4-todo-run2-hint.jsonl，system prompt 加 TODO 提示）— 通過
- v4-TODO 判定 4 項全 PASS：第 2 輪就列 5 項清單（按資料夾＋最後驗證）；成功更新 6 次；最後全 completed；4 次壓縮都帶最新清單。v3 判定全 PASS（91 次工具呼叫、ALL OK、主對話各輪 input 最高 6,497；摘要請求本身最高 11,045）。
- 每次壓縮後第一個動作都是 todo_write 更新清單，接著直接做下一個資料夾。
- read_file 31 → 25：本次壓縮後**沒有重讀**（run1 有 1 次 6 檔重讀）。注意（推論）：本次壓縮都落在 edit 批次之後、而非剛讀完檔時，重讀減少可能主要來自壓縮時機不同，不能全歸功於清單；各 1 次樣本。
- 對照結論（n=1，單次行為觀察，且壓縮時機不同有混淆）：在這個任務、這一次，只靠工具描述 → 0 次使用；system prompt 加一句 → 規劃、更新、壓縮後接續都出現。與 v1「改工具描述比改程式有效」對照：這次是工具描述不夠、要 system prompt。

## v4-3 subagent（task 工具）
- 設計（定案）：task(description, prompt) → harness 開全新對話（SUBAGENT_SYSTEM、SUB_TOOLS＝read_file/grep/glob），最多 20 輪、不壓縮、不能再呼叫 task；只把最後文字回給主對話。sub 的 usage 不碰 LAST_USAGE。log 同檔，sub 行帶 agent=sub、sub_id；summarize_log 既有判定只看主對話行。system prompt 加一句 task 使用時機（記取 TODO 實驗教訓）。
- 實驗題：盤點 mini_repo 折扣函式與呼叫關係；標準答案 fixtures/subagent_answer.md（關鍵：loyalty_bonus 無人呼叫；bulk_restock 是進貨成本）。指揮官 grep 核對無誤。

### v4-3 審查第 1 輪（sonnet，乾淨上下文）
- 驗證：pytest `235 passed`。
- 必改：無（隔離、安全、配對皆確認）。
- 建議採納：subagent API 例外原本 return 離開 _loop，同批已執行的 write_file 結果被丟、close_dangling 誤標「未執行」→ 改成 task 的 is_error 結果、同批繼續；有 sub 行時 v3 照跑、v1 略過並說明；summarize_log exit code 在 FAIL 時沒反映（既有問題）→ 修；chars 是截斷後長度，文件改口徑；兩個空洞測試補強；補「被拒絕在前、task 在後 → 略過」測試。
- 不採納：mini_repo 太小、grep 兩次就能解——刻意保留（答案可客觀核對；比的是主對話 context 有沒有被灌，不是 subagent 是否必要），已寫進文件。
- 修正後驗證：pytest `239 passed`；舊 log 跑 summarize_log 正常。進真實測驗。

### v4-3 真實測驗（evidence/logs/v4-sub-run-task.jsonl vs evidence/logs/v4-sub-run-control.jsonl）— 通過（HANDOFF v4 完成標準）
| | 實驗組（task） | 對照組（自己搜） |
|---|---|---|
| 主對話輪數 | 2 | 7 |
| 主對話各輪 input 最高（不含摘要請求） | 6,026 | 10,164（因此觸發 1 次壓縮，該次摘要請求本身 input 11,452） |
| 主對話自己的工具呼叫 | task×1，之後 0 次重做搜尋 | glob 2、grep 5、read_file 4 |
| subagent | 8 輪、read 13/glob 3/grep 2，in=53k | — |
| 回給主對話 / sub 收到的工具結果 | 2,638 / 18,830 字元（14%） | — |
| 總 tokens | 主 11k＋sub 56k ≈ 67k | 56k |
| 答案 | 正確（loyalty_bonus 無人呼叫、bulk_restock 非銷售折扣、加分項 unit_price_after） | 同樣正確 |
- 總 tokens 拆解（log 實測）：實驗組＝主對話 input 9,569＋output 1,671＝11,240，加 subagent input 53,116＋output 3,190＝56,306，合計約 67.5k；對照組＝主對話 43,235＋壓縮的摘要請求 11,452＋1,619＝13,071，合計 56,306。兩邊都出現 56,306 屬巧合（已核對兩份 log 不同）。
- 結論（n=1，單次量測、設計好的對照）：這個任務、這一次，主對話各輪 input 最高低約 4 成（不含摘要請求）、不用壓縮，答案品質沒掉；總花費反而多約 2 成——subagent 省的是主對話 context，不是錢。
- 兩組都發現任務句寫錯路徑（WORKDIR 裡是 mini_repo/ 不是 sandbox/mini_repo），自行修正並在回答中說明。
- summarize_log 誤判（真實踩到）：實驗組 log 印了 v3 判定且第 4 項顯示 ALL OK——v3 的開關原本是「整份 log 文字含 docstring_repo」，subagent 的 glob(**/*) 輸出含這個字；第 4 項又是讀 sandbox 現況而非 log。修：改看主對話的任務句（first_task_text）。pytest `240 passed`（含新測試）。
- 已知限制（未修）：v3 第 4 項讀 sandbox 現況，事後換過 sandbox 再判舊 log 會不準。

## v4-4 hooks（pre_tool_use／post_tool_use）
- 設計（定案）：`.agent/hooks.json`（專案根、WORKDIR 外、gitignore），main 啟動時載入一次；matcher fullmatch 工具名；shell 指令、cwd＝專案根、stdin JSON、env 同 bash 清理＋AGENT_WORKDIR；exit 0 放行、2 pre 擋下／post 回報（不設 is_error，觀察點）、其他 fail-open 並警告。順序：權限層→pre→工具→post；pre 擋下與 deny 一樣觸發同批略過；subagent 也跑 hooks。
- 實驗：post hook `fixtures/hooks/no_print_hook.py`（shop/ 禁用 print、要 logging），任務句不提規則。

### v4-4 審查第 1 輪（sonnet，乾淨上下文）
- 驗證：pytest `263 passed`；指揮官手動餵 hook 腳本：print → exit 2、logging → exit 0。
- 必改（皆採納）：非 UTF-8 hook 輸出／surrogate payload 丟未接住的例外 → 工具已執行卻被標「未執行」，改 errors=replace、ensure_ascii、except Exception fail-open；hook stderr 無上限整段進 tool_result → 截斷 2000 字。
- 建議（皆採納）：HOOKS_FILE 相對路徑對專案根解析；hook log 直接記 path（judge 優先用、normpath 正規化）；文件補「hook 腳本每次重讀，allow_all 下模型可改腳本」；補 OSError/非 UTF-8/surrogate/巨量輸出測試。
- 修正後驗證：pytest `270 passed`；指揮官實測非 UTF-8 stderr 的 post hook → 回報附加、工具結果保留、不崩。進真實測驗。

### v4-4 真實測驗（evidence/logs/v4-hooks-run1.jsonl）— 通過
- v4-hooks 判定全 PASS：第 3 輪模型用 `print(..., file=sys.stderr)` → hook exit 2 ×2（回報行號＋規則）→ 第 4 輪模型說「依專案規定，把 print 改成 logging」，加 import logging／logger、改 logger.debug → 同批第 1 個 edit 時另一處 print 還在（exit 2），後兩個 edit 後 exit 0。最終 orders.py 無 print。
- post 回報**沒設 is_error**，模型仍注意到並修正——尾端附文字的回饋就足夠。
- 模型自行驗證 A17 失敗是既有的：在 /tmp 做一份去掉改動的副本跑基準（bash 經詢問、使用者回 a）。觀察：這條指令寫到 WORKDIR 外（/tmp），權限層有攔下詢問，屬人核准；同 v3 的「a 等同一次性 y，要讀內容」提醒。

## 交付前：閉門會議第 1 輪（docs/review/01-閉門會議-第1輪-20261024.md）
- 10 位模擬委員，分數 59–76；最低：可重現（證據不出貨）、統計（n=1、混淆）。
- 作業單 19 項全部採納（必改 15、選改 4）；ask 次數、峰值等數字皆以 log 重算。實驗結論降級為與證據相符（v2 拒絕理由混淆、v3 人為門檻下的機制測試、各實驗 n=1）。
- requirements 補 httpx2 並固定範圍；乾淨 venv 實跑 `270 passed`。新增 MIT LICENSE；HANDOFF 署名改 @Jadiouo。
- 使用者決定：GitHub 先 private 再公開；README 中英雙語；repo 名 coding-agent-from-scratch；commit 作者改 noreply、保留歷史、拿掉 session 網址；PROGRESS 與 docs/review 公開＋精選 log 放 evidence/；授權 MIT。
- 未採納／延後：補跑各實驗 3–5 次（統計委員要求）——先以降級措辭處理，之後可補。

## 推上 GitHub（private）
- 改寫歷史：作者／提交者改 `Jadiouo <215644065+Jadiouo@users.noreply.github.com>`、移除 commit 的 Claude-Session 行（保留 Co-Authored-By）；審查檔中的 email 與家目錄字樣也從歷史中移除；scripts/sanitize_logs.py 改為執行時取得家目錄與使用者名，不寫死。本 repo 的 git user.email 設為 noreply。
- 舊歷史只留在本機分支 backup/pre-rewrite（未推送）。
- 推送 main 到 https://github.com/Jadiouo/coding-agent-from-scratch（PRIVATE）。重新 clone 驗證：78 個檔案，無 logs/、sandbox/、.env、.agent；全歷史掃描 email／家目錄／session 網址／key 皆 0。pytest `270 passed`。
- 待使用者：在 GitHub 上看過後自行轉 public。
