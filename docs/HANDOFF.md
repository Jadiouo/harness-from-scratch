# Handoff：自製 Coding Agent（迷你 Claude Code）

Oct 15, 2026 · @Jadio

## 目標與動機

目標是親手寫一個 Claude Code 風格的 coding agent，做完後能完整講清楚 harness 的流程與 agent 的運作方式。重點是理解，不是做出能取代 Claude Code 的產品。

- 成功標準：能在終端機對它下指令，讓它自己讀檔、改檔、跑指令、修正錯誤，完成一個小型程式任務
- 附帶收穫：之後用 Claude Code 或 Agent SDK 時，知道每個行為背後是哪一段 harness 在做

## 核心概念：agent loop

模型本身不會執行任何東西；harness 就是組裝 request、執行工具、管理上下文的那層程式。

（原文此處嵌入一張圖：agent loop · 6 步、1 個判斷）

模型回 `end_turn` 時迴圈結束；只要回 `tool_use`，harness 就執行工具、把結果當 `tool_result` 寫回歷史，再送下一輪。

## 分階段路線圖

每一階段都要能獨立跑起來再進下一階段。做完 v0–v2 就能理解大約八成的 harness。

1. **v0 — 最小迴圈**：agent loop + 3 個工具（`read_file`、`write_file`、`bash`），終端機互動。
   - 完成標準：叫它「建立 hello.py 並執行」，它能自己寫檔、跑、回報結果。
2. **v1 — 讓它會找東西**：加 `edit`（字串替換）、`grep`、`glob`。
   - 完成標準：給它一個陌生小 repo，它會先搜、再讀、再精準修改，而不是整檔覆寫。
3. **v2 — 權限層**：寫檔與危險指令（如 `rm`）執行前先問使用者 y/n。
   - 完成標準：拒絕一次後，模型能收到「被拒絕」的回饋並改走別的路。
4. **v3 — 上下文管理**：工具輸出截斷；對話過長時自動摘要壓縮（compaction）。
   - 完成標準：一個需要 30 輪以上工具呼叫的任務不會爆 context。
5. **v4 — 進階**：subagent（子任務開新 loop、只回傳結論）、TODO 工具、啟動時自動載入 `CLAUDE.md`、hooks。
   - 完成標準：主 agent 能把「搜尋整個 repo」丟給 subagent，自己的 context 保持乾淨。

## 技術選擇與限制

預設用 Python + Anthropic 官方 SDK，直接呼叫 Messages API，不用任何 agent 框架——框架會把要學的部分藏起來。

| 項目 | 選擇 | 理由 |
| --- | --- | --- |
| 語言 | Python | 上手快，SDK 成熟 |
| 模型呼叫 | Messages API + tool use | 迴圈要自己寫，才看得到 harness |
| 介面 | 終端機 REPL | 先不碰 UI，專注在迴圈 |
| 框架 | 不用 | 學習目的，避免黑盒 |

- 費用：只需 API key，不需要 GPU
- 安全：v2 權限層之前，只在丟棄式資料夾或容器裡跑 `bash` 工具
- 開放問題：語言是否改用 TypeScript（Claude Code 本身是 TS）尚未決定

## 觀察重點與常見坑

這個專案的價值在「觀察」，每個階段都刻意做對照實驗。

- **System prompt 的影響**：拿掉或改寫一段，看行為怎麼變
- **工具描述的品質**：描述模糊時模型會用錯工具；改寫描述常比改程式有效
- **錯誤要回傳**：工具失敗時把錯誤訊息當 `tool_result`（標記 is_error）送回，模型才會自己修
- **`tool_use` 與 `tool_result` 要成對**：每個 `tool_use` 都必須在下一則 user 訊息裡有對應 id 的結果，否則 API 會報錯
- **輸出太長**：`bash` 或 `read_file` 一次吐上萬行會吃光 context，v0 就先加截斷
- **無限迴圈**：設最大輪數上限，避免模型卡在同一個錯誤裡一直重試
- **記錄一切**：每輪的 request/response 存成 log，事後回放是最好的學習材料

## 目前狀態與下一步

目前只有規劃，還沒有任何程式碼。下一步是寫出 v0。

- [ ] 建 repo、設定 API key（環境變數，不寫進程式碼）
- [ ] 寫 v0：agent loop + `read_file`、`write_file`、`bash`
- [ ] 用「建立 hello.py 並執行」測試 v0
- [ ] 加上每輪 request/response 的 log
- [ ] 決定語言：Python 或 TypeScript

## 接手原則

一次只做一個階段；每階段跑通並用完成標準驗證後才往下；目標是理解，所以解釋比程式碼量重要。
