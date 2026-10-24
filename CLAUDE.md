# Coding_Agent — 學習用迷你 coding agent

- 規格／路線圖：`docs/HANDOFF.md`
- 進度檔（每輪驗證、審查結果、決定與理由）：`docs/PROGRESS.md`
- 原則：一次只做一個階段，用完成標準驗證後才往下；解釋比程式碼量重要。

## 指令
- 測試（安靜版）：`.venv/bin/python -m pytest -q tests/`
- 執行：`.venv/bin/python agent.py "任務"`（需 `ANTHROPIC_API_KEY`）
- bash 工具的工作目錄固定在 `sandbox/`（`WORKDIR`）；v2 權限層只是字串規則、不是安全邊界，仍只在丟棄式資料夾跑。
